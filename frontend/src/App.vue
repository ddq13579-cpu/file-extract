<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import api from "./api";
import { AxiosError } from "axios";

type Field = {
  field_name: string;
  field_key: string;
  field_type: "text" | "number" | "date" | "boolean";
  description: string;
  required: boolean;
  sort_order: number;
  extraction_type?: "ai_extract" | "calculated";
  calc_rule?: string | null;
  source_field_key?: string | null;
  calc_params?: string | null;
};
type Template = { id: number; name: string; description: string; fields: Field[] };
type SelectedFile = { file: File; path: string };
type Document = { id: number; filename: string; relative_path: string; file_type: string; status: string; template_id: number; updated_at: string; error_message?: string };
type RecordItem = { id: number; document_id: number; template_id: number; status: string; json_data: Record<string, unknown>; updated_at: string };
type ProcessingLog = { id: number; stage: string; level: string; message: string; model_name?: string; prompt_tokens?: number; candidates_tokens?: number; total_tokens?: number; attempt?: number; request_started_at?: string; request_completed_at?: string; duration_ms?: number; response_json?: string; created_at: string };

const isAuthenticated = ref(false);
const username = ref("");
const loginUsername = ref("");
const loginPassword = ref("");
const loginLoading = ref(false);

const page = ref<"upload" | "templates" | "results">("upload");
const templates = ref<Template[]>([]);
const selectedTemplateId = ref<number>();
const selectedFiles = ref<SelectedFile[]>([]);
const documents = ref<Document[]>([]);
const records = ref<RecordItem[]>([]);
const editingTemplate = ref<Template | null>(null);
const dialogVisible = ref(false);
const selectedRecord = ref<RecordItem | null>(null);
const rawText = ref("");
const textDialogVisible = ref(false);
const logsDialogVisible = ref(false);
const selectedLogs = ref<ProcessingLog[]>([]);

const selectedTemplate = computed(() => templates.value.find((template) => template.id === selectedTemplateId.value));
const recordFields = computed(() => templates.value.find((template) => template.id === selectedRecord.value?.template_id)?.fields ?? []);

async function login() {
  if (!loginUsername.value.trim() || !loginPassword.value.trim()) {
    return ElMessage.warning("请输入账号和密码");
  }
  loginLoading.value = true;
  try {
    const res = await api.post<{ token: string; username: string }>("/auth/login", {
      username: loginUsername.value.trim(),
      password: loginPassword.value.trim(),
    });
    localStorage.setItem("token", res.data.token);
    username.value = res.data.username;
    isAuthenticated.value = true;
    loginPassword.value = "";
    ElMessage.success("登录成功");
    await refresh();
    startPolling();
  } catch (error) {
    const detail = error instanceof AxiosError ? error.response?.data?.detail : undefined;
    ElMessage.error(typeof detail === "string" ? detail : "账号或密码错误");
  } finally {
    loginLoading.value = false;
  }
}

function logout() {
  localStorage.removeItem("token");
  isAuthenticated.value = false;
  stopPolling();
  ElMessage.info("已安全退出系统");
}

async function checkAuth() {
  const token = localStorage.getItem("token");
  if (!token) {
    isAuthenticated.value = false;
    return;
  }
  try {
    const res = await api.get<{ username: string }>("/auth/check");
    username.value = res.data.username;
    isAuthenticated.value = true;
    await refresh();
    startPolling();
  } catch {
    localStorage.removeItem("token");
    isAuthenticated.value = false;
  }
}

async function refresh() {
  const [templateResponse, documentResponse, recordResponse] = await Promise.all([api.get<Template[]>("/templates"), api.get<Document[]>("/documents"), api.get<RecordItem[]>("/records")]);
  templates.value = templateResponse.data;
  documents.value = documentResponse.data;
  records.value = recordResponse.data;
  if (!selectedTemplateId.value && templates.value[0]) selectedTemplateId.value = templates.value[0].id;
}

function addFiles(files: FileList | File[]) {
  const additions = Array.from(files).map((file) => ({ file, path: file.webkitRelativePath || file.name }));
  selectedFiles.value = [...selectedFiles.value, ...additions];
}

async function dropFiles(event: DragEvent) {
  event.preventDefault();
  if (event.dataTransfer?.files) addFiles(event.dataTransfer.files);
}

async function upload() {
  if (!selectedTemplateId.value) return ElMessage.warning("请先选择提取模板");
  if (!selectedFiles.value.length) return ElMessage.warning("请选择文件或文件夹");
  const body = new FormData();
  body.append("template_id", String(selectedTemplateId.value));
  selectedFiles.value.forEach(({ file, path }) => { body.append("files", file); body.append("relative_paths", path); });
  const response = await api.post("/files/upload", body);
  selectedFiles.value = [];
  await refresh();
  if (response.data.uploaded.length) await api.post("/files/process", response.data.uploaded.map((item: { id: number }) => item.id));
  ElMessage.success(`已加入 ${response.data.uploaded.length} 个文件；重复或跳过 ${response.data.duplicates.length + response.data.skipped.length} 个`);
  window.setTimeout(refresh, 1500);
}

function newTemplate() {
  editingTemplate.value = { id: 0, name: "", description: "", fields: [] };
  dialogVisible.value = true;
}
function editTemplate(template: Template) {
  editingTemplate.value = JSON.parse(JSON.stringify(template));
  dialogVisible.value = true;
}
function duplicateTemplate(template: Template) {
  const copy: Template = JSON.parse(JSON.stringify(template));
  copy.id = 0;
  copy.name = `${template.name} - 副本`;
  editingTemplate.value = copy;
  dialogVisible.value = true;
}
function onExtractionTypeChange(field: Field) {
  if (field.extraction_type === "calculated") {
    if (!field.calc_rule) field.calc_rule = "age_from_id";
  }
}

function onCalcRuleChange(field: Field) {
  if (field.calc_rule === "range_from_value" && !field.calc_params) {
    field.calc_params = '{"threshold": 5, "gt_value": "大", "lte_value": "小"}';
  } else if (field.calc_rule === "value_map" && !field.calc_params) {
    field.calc_params = '{"map": {"小": 18, "大": 44}}';
  }
}

function addField() {
  if (!editingTemplate.value) return;
  const existingKeys = new Set(editingTemplate.value.fields.map((field) => field.field_key));
  let number = editingTemplate.value.fields.length + 1;
  while (existingKeys.has(`field_${number}`)) number += 1;
  editingTemplate.value.fields.push({
    field_name: "",
    field_key: `field_${number}`,
    field_type: "text",
    description: "",
    required: false,
    sort_order: editingTemplate.value.fields.length,
    extraction_type: "ai_extract",
    calc_rule: null,
    source_field_key: null,
    calc_params: null,
  });
}
function moveField(index: number, offset: number) {
  if (!editingTemplate.value) return;
  const target = index + offset;
  if (target < 0 || target >= editingTemplate.value.fields.length) return;
  const [field] = editingTemplate.value.fields.splice(index, 1);
  editingTemplate.value.fields.splice(target, 0, field);
  editingTemplate.value.fields.forEach((item, order) => { item.sort_order = order; });
}
async function saveTemplate() {
  if (!editingTemplate.value) return;
  if (!editingTemplate.value.name.trim()) {
    ElMessage.warning("请填写模板名称");
    return;
  }
  const invalidField = editingTemplate.value.fields.find((field) => !field.field_name.trim() || !field.field_key.trim());
  if (invalidField) {
    ElMessage.warning("请填写每个字段的中文名称；field_key 已自动生成，可保留不改");
    return;
  }
  const payload = {
    name: editingTemplate.value.name.trim(),
    description: editingTemplate.value.description,
    fields: editingTemplate.value.fields.map((field, index) => ({
      field_name: field.field_name.trim(),
      field_key: field.field_key.trim(),
      field_type: field.field_type,
      description: field.description || "",
      required: Boolean(field.required),
      sort_order: index,
      extraction_type: field.extraction_type || "ai_extract",
      calc_rule: field.extraction_type === "calculated" ? field.calc_rule || null : null,
      source_field_key: field.extraction_type === "calculated" ? field.source_field_key || null : null,
      calc_params: field.extraction_type === "calculated" ? field.calc_params || null : null,
    })),
  };
  const id = editingTemplate.value.id;
  try {
    const response = id ? await api.put<Template>(`/templates/${id}`, payload) : await api.post<Template>("/templates", payload);
    selectedTemplateId.value = response.data.id;
    dialogVisible.value = false;
    await refresh();
    ElMessage.success("模板已保存");
  } catch (error) {
    const detail = error instanceof AxiosError ? error.response?.data?.detail : undefined;
    ElMessage.error(typeof detail === "string" ? detail : "保存模板失败，请检查字段名称和 field_key");
  }
}
async function deleteTemplate(template: Template) {
  try {
    await ElMessageBox.confirm(`删除模板“${template.name}”？`, "确认删除", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await api.delete(`/templates/${template.id}`);
    if (selectedTemplateId.value === template.id) selectedTemplateId.value = undefined;
    await refresh();
    ElMessage.success("模板已删除");
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    const detail = error instanceof AxiosError ? error.response?.data?.detail : undefined;
    ElMessage.error(typeof detail === "string" ? detail : "删除模板失败");
  }
}
async function selectRecord(record: RecordItem) {
  selectedRecord.value = JSON.parse(JSON.stringify(record));
}
async function showRawText(documentId: number) {
  const response = await api.get<{ raw_text: string | null }>(`/documents/${documentId}`);
  rawText.value = response.data.raw_text || "该文件尚未完成文字提取。";
  textDialogVisible.value = true;
}
async function showLogs(documentId: number) {
  const response = await api.get<{ logs: ProcessingLog[] }>(`/documents/${documentId}`);
  selectedLogs.value = response.data.logs;
  logsDialogVisible.value = true;
}
async function saveRecord() {
  if (!selectedRecord.value) return;
  await api.put(`/records/${selectedRecord.value.id}`, { json_data: selectedRecord.value.json_data });
  ElMessage.success("结果已保存");
  await refresh();
}
async function retryDocument(documentId: number) {
  await api.post("/files/process", [documentId]);
  ElMessage.success("文件已重新加入处理队列");
  await refresh();
}
async function retryAllFailedDocuments() {
  const failedCount = documents.value.filter((document) => document.status === "failed").length;
  if (!failedCount) return ElMessage.info("当前没有计算失败的任务");
  try {
    await ElMessageBox.confirm(
      `确认重新开始全部 ${failedCount} 个计算失败的任务？`,
      "重新开始",
      { type: "warning", confirmButtonText: "重新开始", cancelButtonText: "取消" },
    );
    const response = await api.post<{ queued: number }>("/documents/retry-failed");
    ElMessage.success(`已重新加入 ${response.data.queued} 个失败任务`);
    await refresh();
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    const detail = error instanceof AxiosError ? error.response?.data?.detail : undefined;
    ElMessage.error(typeof detail === "string" ? detail : "批量重新开始失败");
  }
}
async function deleteDocument(document: Document) {
  try {
    await ElMessageBox.confirm(
      `删除任务“${document.filename}”及其原始文件、OCR 文字和结构化结果？此操作无法恢复。`,
      "确认删除任务",
      { type: "warning", confirmButtonText: "删除任务", cancelButtonText: "取消" },
    );
    await api.delete(`/documents/${document.id}`);
    if (selectedRecord.value?.document_id === document.id) selectedRecord.value = null;
    ElMessage.success("任务已删除");
    await refresh();
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    const detail = error instanceof AxiosError ? error.response?.data?.detail : undefined;
    ElMessage.error(typeof detail === "string" ? detail : "删除任务失败");
  }
}
async function deleteAllDocuments() {
  if (!documents.value.length) return ElMessage.warning("当前没有任务可删除");
  try {
    await ElMessageBox.confirm(
      "确认删除所有任务及其原始文件、OCR 文字和提取结果？此操作无法恢复。",
      "确认删除所有任务",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    const response = await api.delete<{ deleted: number; skipped: number }>("/documents");
    selectedRecord.value = null;
    ElMessage.success(`已删除 ${response.data.deleted} 个任务${response.data.skipped ? `；跳过 ${response.data.skipped} 个正在处理的任务` : ""}`);
    await refresh();
  } catch (error) {
    if (error === "cancel" || error === "close") return;
    const detail = error instanceof AxiosError ? error.response?.data?.detail : undefined;
    ElMessage.error(typeof detail === "string" ? detail : "删除任务失败");
  }
}
function exportExcel() {
  if (!selectedTemplateId.value) return ElMessage.warning("请选择模板");
  const token = localStorage.getItem("token") || "";
  window.open(`/api/export/excel?template_id=${selectedTemplateId.value}&token=${encodeURIComponent(token)}`, "_blank");
}
function openDocument(documentId: number) {
  const token = localStorage.getItem("token") || "";
  window.open(`/api/documents/${documentId}/file?token=${encodeURIComponent(token)}`, "_blank");
}
function recordValue(key: string): string {
  const value = selectedRecord.value?.json_data[key];
  return value === null || value === undefined ? "" : String(value);
}
function updateRecordValue(key: string, value: string) {
  if (selectedRecord.value) selectedRecord.value.json_data[key] = value;
}

let refreshTimer: number | undefined;

function startPolling() {
  stopPolling();
  refreshTimer = window.setInterval(() => void refresh(), 5000);
}

function stopPolling() {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = undefined;
  }
}

function handleAuthExpired() {
  isAuthenticated.value = false;
  stopPolling();
  ElMessage.warning("登录凭证已失效，请重新登录");
}

onMounted(async () => {
  window.addEventListener("auth-expired", handleAuthExpired);
  await checkAuth();
});

onBeforeUnmount(() => {
  window.removeEventListener("auth-expired", handleAuthExpired);
  stopPolling();
});
</script>

<template>
  <div v-if="!isAuthenticated" class="login-wrapper">
    <el-card class="login-card">
      <template #header>
        <div class="login-header">
          <h2>文档智能整理系统</h2>
          <p>请先验证账号密码以继续访问</p>
        </div>
      </template>
      <el-form @keyup.enter="login" label-position="top">
        <el-form-item label="账号">
          <el-input v-model="loginUsername" placeholder="请输入管理员账号" size="large" clearable />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="loginPassword" type="password" placeholder="请输入密码" size="large" show-password clearable />
        </el-form-item>
        <el-button type="primary" size="large" class="login-btn" :loading="loginLoading" @click="login">
          安全登录
        </el-button>
      </el-form>
    </el-card>
  </div>

  <template v-else>
    <el-container class="shell">
      <el-header>
        <strong>文档智能整理系统</strong>
        <el-menu mode="horizontal" :default-active="page" @select="(value: string) => page = value as 'upload' | 'templates' | 'results'">
          <el-menu-item index="upload">文件处理</el-menu-item>
          <el-menu-item index="templates">模板管理</el-menu-item>
          <el-menu-item index="results">处理结果</el-menu-item>
        </el-menu>
        <div class="user-info">
          <span class="username">👤 {{ username }}</span>
          <el-button size="small" type="info" plain @click="logout">退出登录</el-button>
        </div>
      </el-header>
      <el-main>
        <section v-if="page === 'upload'" class="panel">
          <h2>上传并处理文件</h2>
          <el-select v-model="selectedTemplateId" placeholder="选择提取模板"><el-option v-for="template in templates" :key="template.id" :label="template.name" :value="template.id" /></el-select>
          <div class="dropzone" @dragover.prevent @drop="dropFiles">
            <div>将文件或文件夹拖到这里，或点击选择</div><small>支持 PDF / JPG / PNG / WEBP / BMP / TIFF / HEIC</small>
          </div>
          <input type="file" multiple webkitdirectory @change="addFiles(($event.target as HTMLInputElement).files!)" />
          <p>已选择 {{ selectedFiles.length }} 个文件</p>
          <el-button type="primary" :disabled="!templates.length" @click="upload">开始处理</el-button>
        </section>

        <section v-else-if="page === 'templates'" class="panel">
          <el-button type="primary" @click="newTemplate">创建模板</el-button>
          <el-table :data="templates" class="spaced"><el-table-column prop="name" label="名称" /><el-table-column prop="description" label="说明" /><el-table-column label="字段数"><template #default="{ row }">{{ row.fields.length }}</template></el-table-column><el-table-column label="操作"><template #default="{ row }"><el-button link @click="editTemplate(row)">编辑</el-button><el-button link @click="duplicateTemplate(row)">复制</el-button><el-button link type="danger" @click="deleteTemplate(row)">删除</el-button></template></el-table-column></el-table>
        </section>

        <section v-else class="panel">
          <el-button @click="refresh">刷新状态</el-button>
          <div class="right">
            <el-button type="warning" style="margin-right: 12px" :disabled="!documents.some(document => document.status === 'failed')" @click="retryAllFailedDocuments">重新开始失败任务</el-button>
            <el-button type="danger" style="margin-right: 12px" @click="deleteAllDocuments">删除所有任务</el-button>
            <el-button type="success" @click="exportExcel">导出当前模板 Excel</el-button>
          </div>
          <el-table :data="documents" class="spaced"><el-table-column prop="filename" label="文件名" /><el-table-column prop="relative_path" label="来源目录" /><el-table-column prop="file_type" label="类型" /><el-table-column prop="status" label="状态" /><el-table-column label="查看"><template #default="{ row }"><el-button link @click="openDocument(row.id)">原文件</el-button><el-button link @click="showRawText(row.id)">原始文字</el-button><el-button link @click="showLogs(row.id)">处理日志</el-button><el-button v-if="row.status === 'failed'" link type="warning" @click="retryDocument(row.id)">重新处理</el-button><el-button link type="danger" :disabled="row.status === 'processing' || row.status === 'ai_processing'" @click="deleteDocument(row)">删除任务</el-button></template></el-table-column></el-table>
        </section>
      </el-main>
    </el-container>

    <el-dialog v-model="dialogVisible" :title="editingTemplate?.id ? '编辑模板' : '创建模板'" width="85%" destroy-on-close>
      <el-form v-if="editingTemplate" label-width="90px"><el-form-item label="名称"><el-input v-model="editingTemplate.name" placeholder="请输入模板名称" /></el-form-item><el-form-item label="说明"><el-input v-model="editingTemplate.description" placeholder="模板适用场景说明" /></el-form-item>
        <h3>提取字段 <el-button link type="primary" @click="addField">添加字段</el-button></h3>
        <p class="field-help">可以在每个字段中选择由“AI 提取”或“程序自动计算”。程序自动计算字段不依赖 AI 提取，完全由后台算法计算得出。</p>
        
        <div v-for="(field, index) in editingTemplate.fields" :key="index" class="field-card">
          <div class="field-row-main">
            <el-input v-model="field.field_name" placeholder="中文字段名称，如：姓名" style="width: 180px" />
            <el-input v-model="field.field_key" placeholder="系统标识（如 field_1）" style="width: 160px" />
            <el-select v-model="field.field_type" placeholder="类型" style="width: 90px">
              <el-option value="text" label="文本" />
              <el-option value="number" label="数字" />
              <el-option value="date" label="日期" />
              <el-option value="boolean" label="布尔" />
            </el-select>
            <el-select v-model="field.extraction_type" placeholder="生成方式" style="width: 150px" @change="onExtractionTypeChange(field)">
              <el-option value="ai_extract" label="🤖 AI 图片/文档提取" />
              <el-option value="calculated" label="⚡ 程序自动计算" />
            </el-select>
            <el-checkbox v-model="field.required">必填</el-checkbox>
            <div class="field-actions">
              <el-button link :disabled="index === 0" @click="moveField(index, -1)">上移</el-button>
              <el-button link :disabled="index === editingTemplate.fields.length - 1" @click="moveField(index, 1)">下移</el-button>
              <el-button link type="danger" @click="editingTemplate.fields.splice(index, 1)">删除</el-button>
            </div>
          </div>

          <div v-if="field.extraction_type !== 'calculated'" class="field-row-sub">
            <el-input v-model="field.description" placeholder="字段说明 / AI提取提示词（可选，例如：‘投保人信息’后的姓名）" />
          </div>

          <div v-else class="field-row-sub calc-config">
            <el-select v-model="field.calc_rule" placeholder="选择计算规则" style="width: 200px" @change="onCalcRuleChange(field)">
              <el-option value="age_from_id" label="从身份证号算周岁" />
              <el-option value="range_from_value" label="根据数值划分区间(大/小)" />
              <el-option value="value_map" label="字典/条件数值映射" />
            </el-select>
            <el-select v-model="field.source_field_key" placeholder="选择依据字段" style="width: 180px">
              <el-option
                v-for="other in editingTemplate.fields.filter(f => f.field_key !== field.field_key)"
                :key="other.field_key"
                :value="other.field_key"
                :label="other.field_name ? `${other.field_name} (${other.field_key})` : other.field_key"
              />
            </el-select>
            <el-input
              v-if="field.calc_rule === 'range_from_value' || field.calc_rule === 'value_map'"
              v-model="field.calc_params"
              :placeholder="field.calc_rule === 'range_from_value' ? '参数如: {&quot;threshold&quot;:5,&quot;gt_value&quot;:&quot;大&quot;,&quot;lte_value&quot;:&quot;小&quot;}' : '映射如: {&quot;map&quot;:{&quot;小&quot;:18,&quot;大&quot;:44}}'"
              style="flex: 1"
            />
            <span v-else-if="field.calc_rule === 'age_from_id'" class="calc-hint">💡 自动抓取依据字段里的身份证号，按北京时间当前日期精确计算周岁</span>
          </div>
        </div>
      </el-form><template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" @click="saveTemplate">保存</el-button></template>
    </el-dialog>
    <el-dialog v-model="textDialogVisible" title="原始文字" width="70%"><pre class="raw-text">{{ rawText }}</pre></el-dialog>
    <el-dialog v-model="logsDialogVisible" title="处理日志" width="80%">
      <el-table :data="selectedLogs">
        <el-table-column prop="request_started_at" label="请求开始" width="180" />
        <el-table-column prop="request_completed_at" label="请求结束" width="180" />
        <el-table-column label="耗时" width="90"><template #default="{ row }">{{ row.duration_ms != null ? `${row.duration_ms} ms` : "-" }}</template></el-table-column>
        <el-table-column label="次数" width="60"><template #default="{ row }">{{ row.attempt || "-" }}</template></el-table-column>
        <el-table-column prop="stage" label="阶段" width="90" />
        <el-table-column prop="message" label="消息" />
        <el-table-column label="实际模型（SDK）" width="180"><template #default="{ row }">{{ row.model_name || "-" }}</template></el-table-column>
        <el-table-column label="Token（输入/输出/总计）" width="190"><template #default="{ row }">{{ row.prompt_tokens ?? "-" }} / {{ row.candidates_tokens ?? "-" }} / {{ row.total_tokens ?? "-" }}</template></el-table-column>
        <el-table-column label="原始 JSON" min-width="260"><template #default="{ row }"><pre v-if="row.response_json" class="log-json">{{ row.response_json }}</pre><span v-else>-</span></template></el-table-column>
      </el-table>
    </el-dialog>
  </template>
</template>

<style scoped>
.login-wrapper { display: flex; justify-content: center; align-items: center; min-height: 100vh; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
.login-card { width: 400px; padding: 20px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15); }
.login-header { text-align: center; margin-bottom: 10px; }
.login-header h2 { margin: 0; color: #303133; font-size: 22px; }
.login-header p { margin: 6px 0 0; color: #909399; font-size: 13px; }
.login-btn { width: 100%; margin-top: 10px; }

.shell { min-height: 100vh; background: #f5f7fa; }
.el-header { display: flex; align-items: center; gap: 36px; background: white; border-bottom: 1px solid #ddd; }
.el-menu { border: 0; flex: 1; }
.user-info { display: flex; align-items: center; gap: 12px; }
.username { font-size: 14px; color: #606266; font-weight: 500; }
.panel { background: white; padding: 24px; border-radius: 8px; }
.dropzone { margin: 20px 0; padding: 42px; border: 2px dashed #a8abb2; border-radius: 8px; text-align: center; color: #606266; }
.spaced { margin-top: 20px; }
.result-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.right { float: right; }
.field-help { color: #909399; font-size: 13px; margin-bottom: 12px; }
.field-card { border: 1px solid #e4e7ed; border-radius: 8px; padding: 12px; margin-bottom: 12px; background: #fafafa; }
.field-row-main { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.field-row-sub { margin-top: 8px; }
.calc-config { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; background: #f0f9eb; padding: 8px; border-radius: 6px; }
.calc-hint { font-size: 12px; color: #67c23a; }
.field-actions { display: flex; align-items: center; gap: 4px; margin-left: auto; }
.raw-text { max-height: 60vh; overflow: auto; white-space: pre-wrap; }
.log-json { max-height: 120px; overflow: auto; white-space: pre-wrap; margin: 0; font-size: 12px; }
@media (max-width: 800px) { .result-layout { grid-template-columns: 1fr; } .field-row-main { flex-direction: column; align-items: stretch; } }
</style>
