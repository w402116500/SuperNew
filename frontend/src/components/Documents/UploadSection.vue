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

    <div
      v-if="documentStore.selectedFiles.length"
      :class="['selected-file', { 'selected-file-batch': documentStore.selectedFiles.length > 1 }]"
    >
      <span class="selected-file-icon"><i class="fa-regular fa-file-lines"></i></span>
      <span class="selected-file-copy">
        <strong>{{ documentStore.selectedFiles.length === 1 ? documentStore.selectedFiles[0].name : `${documentStore.selectedFiles.length} 个文件` }}</strong>
        <small>{{ documentStore.selectedFiles.length === 1 ? formatFileSize(documentStore.selectedFiles[0].size) : '批量文件 · 等待上传' }}</small>
      </span>
      <div v-if="documentStore.selectedFiles.length > 1" class="selected-file-names">
        <span v-for="file in documentStore.selectedFiles" :key="file.name + file.size">{{ file.name }}</span>
      </div>
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
      v-if="!hasBatchJobs && documentStore.selectedFiles.length <= 1 && documentStore.uploadSteps.length"
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

    <div
      v-if="hasBatchJobs"
      :class="['upload-progress', 'upload-progress-batch', { collapsed: documentStore.uploadProgressCollapsed }]"
    >
      <button type="button" class="upload-progress-header" @click="onToggleCollapse">
        <span>
          <strong>批量处理 {{ documentStore.uploadJobs.length }} 个文件</strong>
          <small>{{ completedJobCount }} / {{ documentStore.uploadJobs.length }} 个文件完成 · {{ batchProgress }}%</small>
        </span>
        <span class="upload-toggle">
          {{ documentStore.uploadProgressCollapsed ? '展开' : '收起' }}
          <i :class="documentStore.uploadProgressCollapsed ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-up'"></i>
        </span>
      </button>

      <div v-show="!documentStore.uploadProgressCollapsed" class="batch-overall-progress">
        <div class="batch-overall-track">
          <div class="batch-overall-fill" :style="{ width: batchProgress + '%' }"></div>
        </div>
        <span>{{ batchStatusLabel }}</span>
      </div>

      <div v-show="!documentStore.uploadProgressCollapsed" class="upload-batch-list">
        <article
          v-for="job in documentStore.uploadJobs"
          :key="job.job_id"
          :class="['upload-batch-item', 'upload-batch-item-' + job.status]"
        >
          <span class="batch-file-icon"><i :class="jobIcon(job.status)"></i></span>
          <div class="batch-file-body">
            <div class="batch-file-heading">
              <strong :title="job.filename">{{ job.filename }}</strong>
              <span class="batch-file-status">{{ jobStatusLabel(job.status) }}</span>
            </div>
            <div class="batch-file-meta">
              <span>{{ jobStepLabel(job) }}</span>
              <span>{{ jobProgress(job) }}%</span>
            </div>
            <div class="batch-file-track">
              <div class="batch-file-fill" :style="{ width: jobProgress(job) + '%' }"></div>
            </div>
            <small v-if="job.status === 'failed' || job.status === 'interrupted'">{{ job.error || job.message }}</small>
          </div>
        </article>
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

const hasBatchJobs = computed(() => documentStore.uploadJobs.length > 1);

const completedJobCount = computed(() =>
  documentStore.uploadJobs.filter((job: any) => job.status === 'completed').length
);

const jobProgress = (job: any) => {
  if (job.status === 'completed') return 100;
  if (job.status === 'failed' || job.status === 'interrupted') {
    const current = job.steps?.find((step: UploadStep) => step.key === job.current_step);
    return Math.max(0, Math.min(100, Number(current?.percent || 0)));
  }
  const current = job.steps?.find((step: UploadStep) => step.status === 'running')
    || job.steps?.find((step: UploadStep) => step.key === job.current_step);
  return Math.max(0, Math.min(100, Number(current?.percent || 0)));
};

const batchProgress = computed(() => {
  if (!documentStore.uploadJobs.length) return 0;
  const total = documentStore.uploadJobs.reduce((sum: number, job: any) => sum + jobProgress(job), 0);
  return Math.round(total / documentStore.uploadJobs.length);
});

const batchStatusLabel = computed(() => {
  const failed = documentStore.uploadJobs.filter((job: any) => ['failed', 'interrupted'].includes(job.status)).length;
  if (failed) return `${failed} 个文件需要处理`;
  if (completedJobCount.value === documentStore.uploadJobs.length) return '全部文件处理完成';
  return '文件正在依次处理';
});

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

const jobStatusLabel = (status: string) => {
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'interrupted') return '已中断';
  if (status === 'pending') return '排队中';
  return '处理中';
};

const jobStepLabel = (job: any) => {
  if (job.status === 'completed') return '处理完成';
  if (job.status === 'failed' || job.status === 'interrupted') return job.message || '处理未完成';
  const current = job.steps?.find((step: UploadStep) => step.status === 'running')
    || job.steps?.find((step: UploadStep) => step.key === job.current_step);
  return current?.label || '等待处理';
};

const jobIcon = (status: string) => {
  if (status === 'completed') return 'fa-solid fa-check';
  if (status === 'failed') return 'fa-solid fa-xmark';
  if (status === 'interrupted') return 'fa-solid fa-pause';
  if (status === 'running') return 'fa-solid fa-spinner fa-spin';
  return 'fa-solid fa-clock';
};
</script>
