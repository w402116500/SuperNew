<template>
  <section class="upload-section">
    <div class="upload-section-head">
      <span class="upload-title-icon"><i class="fa-solid fa-cloud-arrow-up"></i></span>
      <div>
        <h2>快速入库</h2>
        <p>解析结构、三级分块并写入混合索引。</p>
      </div>
    </div>

    <input
      ref="fileInputRef"
      type="file"
      multiple
      accept=".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.webp,.bmp,.tiff,.tif,.html,.htm,.md,.txt"
      hidden
      @change="onFileSelect"
    />

    <button
      type="button"
      class="upload-dropzone"
      @click="triggerFileSelect"
      @dragover.prevent
      @drop.prevent="onFileDrop"
    >
      <span class="dropzone-icon"><i class="fa-solid fa-arrow-up-from-bracket"></i></span>
      <strong>{{ documentStore.selectedFiles.length > 1 ? `${documentStore.selectedFiles.length} 个文件` : (documentStore.selectedFile ? documentStore.selectedFile.name : '拖放文件到这里') }}</strong>
      <span>
        {{ documentStore.selectedFiles.length > 1
          ? '已选择多个文件'
          : documentStore.selectedFile
          ? formatFileSize(documentStore.selectedFile.size)
          : '或点击选择 PDF、DOCX、PPTX、XLSX、图片、HTML、Markdown、TXT 文件' }}
      </span>
    </button>

    <div v-if="documentStore.selectedFiles.length" class="selected-file">
      <span class="selected-file-icon"><i class="fa-regular fa-file-lines"></i></span>
      <span class="selected-file-copy">
        <strong>{{ documentStore.selectedFiles.length === 1 ? documentStore.selectedFiles[0].name : `${documentStore.selectedFiles.length} 个文件` }}</strong>
        <small>{{ documentStore.selectedFiles.length === 1 ? formatFileSize(documentStore.selectedFiles[0].size) : '批量文件 · 等待上传' }}</small>
      </span>
      <button
        type="button"
        class="btn-primary"
        :disabled="documentStore.isUploading"
        @click="onUpload"
      >
        <i :class="documentStore.isUploading ? 'fa-solid fa-spinner fa-spin' : 'fa-solid fa-arrow-up'"></i>
        {{ documentStore.isUploading ? '处理中' : '开始上传' }}
      </button>
    </div>

    <div
      v-if="documentStore.uploadSteps.length"
      :class="['upload-progress', { collapsed: documentStore.uploadProgressCollapsed }]"
    >
      <button type="button" class="upload-progress-header" @click="onToggleCollapse">
        <span>
          <strong>{{ documentStore.uploadProgress || '上传进度' }}</strong>
          <small>{{ completedSteps }} / {{ documentStore.uploadSteps.length }} 个阶段完成</small>
        </span>
        <span class="upload-toggle">
          {{ documentStore.uploadProgressCollapsed ? '展开' : '收起' }}
          <i :class="documentStore.uploadProgressCollapsed ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-up'"></i>
        </span>
      </button>

      <div v-show="!documentStore.uploadProgressCollapsed" class="upload-step-list">
        <div
          v-for="step in documentStore.uploadSteps"
          :key="step.key"
          :class="['upload-step', 'upload-step-' + step.status]"
        >
          <div class="upload-step-header">
            <span class="upload-step-label">
              <i :class="stepIcon(step.status)"></i>
              {{ step.label }}
            </span>
            <span class="upload-step-percent">{{ step.percent }}%</span>
          </div>
          <div class="upload-step-bar">
            <div class="upload-step-fill" :style="{ width: step.percent + '%' }"></div>
          </div>
          <div v-if="step.message" class="upload-step-message">{{ step.message }}</div>
        </div>
      </div>
    </div>

    <div v-if="documentStore.uploadJobs.length > 1" class="upload-batch-list">
      <div v-for="job in documentStore.uploadJobs" :key="job.job_id" class="upload-batch-item">
        <strong>{{ job.filename }}</strong>
        <span>{{ job.status === 'completed' ? '已完成' : job.status === 'failed' ? '失败' : job.status === 'interrupted' ? '已中断' : '处理中' }}</span>
        <small>{{ job.message }}</small>
      </div>
    </div>

    <div class="upload-pipeline-note">
      <div><span>01</span><p><strong>结构解析</strong><small>识别章节、表格与页面</small></p></div>
      <div><span>02</span><p><strong>三级分块</strong><small>保留父子上下文关系</small></p></div>
      <div><span>03</span><p><strong>混合索引</strong><small>Dense + BM25 同步写入</small></p></div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useDocumentStore } from '@/stores/documents';
import type { UploadStep } from '@/types/document';

const documentStore = useDocumentStore();
const fileInputRef = ref<HTMLInputElement | null>(null);

onMounted(() => {
  documentStore.loadUploadJobs();
});

const completedSteps = computed(() =>
  documentStore.uploadSteps.filter((step) => step.status === 'completed').length
);

const triggerFileSelect = () => {
  fileInputRef.value?.click();
};

const setSelectedFiles = (files: File[]) => {
  documentStore.selectedFiles = files;
  documentStore.selectedFile = files[0] || null;
  documentStore.uploadProgress = '';
  documentStore.uploadSteps = documentStore.createUploadSteps();
  documentStore.uploadProgressCollapsed = false;
  documentStore.activeUploadJobId = '';
  documentStore.activeUploadJobIds = [];
  documentStore.uploadJobs = [];
};

const onFileSelect = (event: Event) => {
  const files = (event.target as HTMLInputElement).files;
  if (files?.length) setSelectedFiles(Array.from(files));
};

const onFileDrop = (event: DragEvent) => {
  const files = event.dataTransfer?.files;
  if (files?.length) setSelectedFiles(Array.from(files));
};

const onUpload = async () => {
  try {
    await documentStore.uploadDocument();
  } catch (error: any) {
    alert('上传文档失败：' + error.message);
  }
};

const onToggleCollapse = () => {
  documentStore.uploadProgressCollapsed = !documentStore.uploadProgressCollapsed;
};

const formatFileSize = (bytes: number) => {
  if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
};

const stepIcon = (status: UploadStep['status']) => {
  if (status === 'completed') return 'fa-solid fa-check';
  if (status === 'running') return 'fa-solid fa-spinner fa-spin';
  if (status === 'failed') return 'fa-solid fa-xmark';
  return 'fa-solid fa-circle';
};
</script>
