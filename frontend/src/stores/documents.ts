import { defineStore } from 'pinia';
import api from '@/utils/api';
import type { DocumentItem, UploadStep, ActiveDeleteJob, DeleteStep } from '@/types/document';

export const useDocumentStore = defineStore('documents', {
  state: () => ({
    documents: [] as DocumentItem[],
    documentsLoading: false,
    selectedFile: null as File | null,
    selectedFiles: [] as File[],
    isUploading: false,
    uploadProgress: '',
    uploadSteps: [] as UploadStep[],
    uploadProgressCollapsed: false,
    activeUploadJobId: '',
    activeUploadJobIds: [] as string[],
    uploadJobs: [] as any[],
    uploadPollTimer: null as any,
    deleteJobs: {} as Record<string, ActiveDeleteJob>,
    deletePollTimers: {} as Record<string, any>,
    deleteRemoveTimers: {} as Record<string, any>,
  }),

  actions: {
    createUploadSteps(): UploadStep[] {
      return [
        { key: 'upload', label: '文档上传', percent: 0, status: 'pending', message: '' },
        { key: 'mineru', label: 'MinerU 转 Markdown', percent: 0, status: 'pending', message: '' },
        { key: 'chunk', label: 'Markdown 三级分块', percent: 0, status: 'pending', message: '' },
        { key: 'cleanup', label: '替换旧版本', percent: 0, status: 'pending', message: '' },
        { key: 'parent_store', label: '父级分块入库', percent: 0, status: 'pending', message: '' },
        { key: 'vector_store', label: '向量化入库', percent: 0, status: 'pending', message: '' },
      ];
    },

    createDeleteSteps(): DeleteStep[] {
      return [
        { key: 'prepare', label: '准备删除', percent: 0, status: 'pending', message: '' },
        { key: 'bm25', label: '同步 BM25 统计', percent: 0, status: 'pending', message: '' },
        { key: 'milvus', label: '删除向量数据', percent: 0, status: 'pending', message: '' },
        { key: 'parent_store', label: '删除父级分块', percent: 0, status: 'pending', message: '' },
      ];
    },

    updateUploadStep(key: string, percent: number, status: UploadStep['status'] = 'running', message = '') {
      if (!this.uploadSteps.length) {
        this.uploadSteps = this.createUploadSteps();
      }
      const idx = this.uploadSteps.findIndex((step) => step.key === key);
      if (idx === -1) return;
      this.uploadSteps[idx] = {
        ...this.uploadSteps[idx],
        percent: Math.max(0, Math.min(100, Math.round(percent || 0))),
        status,
        message,
      };
    },

    mergeDocumentsWithActiveDeletes(nextDocuments: DocumentItem[]): DocumentItem[] {
      const merged = Array.isArray(nextDocuments) ? [...nextDocuments] : [];
      Object.keys(this.deleteJobs).forEach((filename) => {
        const job = this.deleteJobs[filename];
        if (!job || job.status === 'failed') return;
        const exists = merged.some((doc) => doc.filename === filename);
        if (!exists) {
          const currentDoc = this.documents.find((doc) => doc.filename === filename);
          if (currentDoc) {
            merged.push(currentDoc);
          }
        }
      });
      return merged;
    },

    async loadDocuments() {
      this.documentsLoading = true;
      try {
        const response = await api.get('/documents');
        this.documents = this.mergeDocumentsWithActiveDeletes(response.data.documents || []);
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '加载文档列表失败';
        throw new Error(errMsg);
      } finally {
        this.documentsLoading = false;
      }
    },

    async loadUploadJobs() {
      try {
        const response = await api.get('/documents/upload/jobs');
        this.uploadJobs = Array.isArray(response.data) ? response.data : [];
        const activeIds = this.uploadJobs
          .filter((job: any) => ['pending', 'running'].includes(job.status))
          .map((job: any) => job.job_id)
          .filter(Boolean);
        if (activeIds.length) {
          this.activeUploadJobIds = activeIds;
          this.activeUploadJobId = activeIds[0];
          this.isUploading = true;
          this.startUploadJobPolling(activeIds);
        }
      } catch {
        // 任务恢复失败不应阻止文档列表和上传页面使用。
      }
    },

    async uploadDocument() {
      const files = this.selectedFiles.length ? this.selectedFiles : (this.selectedFile ? [this.selectedFile] : []);
      if (!files.length) {
        throw new Error('请先选择文件');
      }

      this.isUploading = true;
      this.uploadProgress = '正在上传...';
      this.uploadSteps = this.createUploadSteps();
      this.uploadProgressCollapsed = false;
      this.updateUploadStep('upload', 0, 'running', '准备上传');

      const formData = new FormData();
      files.forEach((file) => formData.append('file', file));

      try {
        const response = await api.post('/documents/upload/async', formData, {
          headers: {
            'Content-Type': 'multipart/form-data',
          },
          onUploadProgress: (progressEvent) => {
            if (!progressEvent.total) return;
            const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
            this.updateUploadStep('upload', percent, 'running', `已上传 ${percent}%`);
          },
        });

        const data = response.data;
        this.updateUploadStep('upload', 100, 'completed', '文档上传完成');
        this.uploadProgress = data.message;
        const jobs = Array.isArray(data.jobs) ? data.jobs : [data];
        this.activeUploadJobIds = jobs.map((job: any) => job.job_id).filter(Boolean);
        this.uploadJobs = jobs;
        this.activeUploadJobId = this.activeUploadJobIds[0] || '';
        this.startUploadJobPolling(this.activeUploadJobIds);
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '上传失败';
        this.updateUploadStep('upload', 100, 'failed', errMsg);
        this.uploadProgress = '上传失败：' + errMsg;
        this.isUploading = false;
        throw new Error(errMsg);
      }
    },

    syncUploadJob(job: any) {
      this.activeUploadJobId = job.job_id;
      this.uploadProgress = job.message || '';
      if (Array.isArray(job.steps)) {
        this.uploadSteps = job.steps.map((step: any) => ({
          key: step.key,
          label: step.label,
          percent: step.percent,
          status: step.status,
          message: step.message || '',
        }));
      }
      if (job.status === 'completed') {
        this.uploadProgressCollapsed = true;
      }
    },

    startUploadJobPolling(jobIds: string[] | string) {
      this.stopUploadJobPolling();
      const ids = Array.isArray(jobIds) ? jobIds : [jobIds];
      const finished = new Set<string>();

      const poll = async () => {
        try {
          const jobs = await Promise.all(ids.map(async (id) => {
            const response = await api.get(`/documents/upload/jobs/${encodeURIComponent(id)}`);
            return response.data;
          }));
          jobs.forEach((job) => {
            const index = this.uploadJobs.findIndex((item: any) => item.job_id === job.job_id);
            if (index >= 0) this.uploadJobs[index] = job;
            else this.uploadJobs.push(job);
            if (['completed', 'failed', 'interrupted'].includes(job.status)) finished.add(job.job_id);
          });
          const active = jobs.find((job) => job.status === 'running' || job.status === 'pending') || jobs[0];
          if (active) this.syncUploadJob(active);

          if (finished.size === ids.length) {
            this.stopUploadJobPolling();
            this.isUploading = false;
            this.selectedFile = null;
            this.selectedFiles = [];
            await this.loadDocuments();
          }
        } catch (error: any) {
          this.uploadProgress = '进度查询失败：' + (error.response?.data?.detail || error.message);
          this.stopUploadJobPolling();
          this.isUploading = false;
        }
      };

      poll();
      this.uploadPollTimer = setInterval(poll, 1000);
    },

    stopUploadJobPolling() {
      if (this.uploadPollTimer) {
        clearInterval(this.uploadPollTimer);
        this.uploadPollTimer = null;
      }
    },

    isDeletingDocument(filename: string): boolean {
      const job = this.deleteJobs[filename];
      return !!(job && job.status === 'running');
    },

    isDeleteActionLocked(filename: string): boolean {
      const job = this.deleteJobs[filename];
      return !!(job && (job.status === 'running' || job.status === 'completed'));
    },

    getDeleteButtonIcon(filename: string): string {
      const job = this.deleteJobs[filename];
      if (job?.status === 'running') return 'fas fa-spinner fa-spin';
      if (job?.status === 'completed') return 'fas fa-check';
      return 'fas fa-trash';
    },

    setDeleteJob(filename: string, nextJob: Partial<ActiveDeleteJob>) {
      this.deleteJobs = {
        ...this.deleteJobs,
        [filename]: {
          ...(this.deleteJobs[filename] || {
            status: 'running',
            message: '',
            collapsed: false,
            steps: this.createDeleteSteps(),
          }),
          ...nextJob,
        },
      };
    },

    syncDeleteJob(filename: string, job: any) {
      const current = this.deleteJobs[filename] || {};
      this.setDeleteJob(filename, {
        jobId: job.job_id,
        status: job.status,
        message: job.message || '',
        collapsed: job.status === 'completed' ? true : Boolean(current.collapsed),
        steps: Array.isArray(job.steps)
          ? job.steps.map((step: any) => ({
              key: step.key,
              label: step.label,
              percent: step.percent,
              status: step.status,
              message: step.message || '',
            }))
          : this.createDeleteSteps(),
      });
    },

    async deleteDocument(filename: string) {
      if (this.isDeletingDocument(filename)) {
        return;
      }
      if (!confirm(`确定要删除文档 "${filename}" 吗？这将同时删除 Milvus 中的所有相关向量。`)) {
        return;
      }

      this.clearDeleteRemovalTimer(filename);
      this.setDeleteJob(filename, {
        status: 'running',
        message: '正在提交删除任务...',
        collapsed: false,
        steps: this.createDeleteSteps().map((step) =>
          step.key === 'prepare'
            ? { ...step, percent: 1, status: 'running' as const, message: '正在提交删除任务' }
            : step
        ),
      });

      try {
        const response = await api.delete(`/documents/delete/async/${encodeURIComponent(filename)}`);
        const data = response.data;
        this.setDeleteJob(filename, {
          jobId: data.job_id,
          status: 'running',
          message: data.message || `正在删除 ${filename}`,
          collapsed: false,
        });
        this.startDeleteJobPolling(filename, data.job_id);
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '删除请求失败';
        this.setDeleteJob(filename, {
          status: 'failed',
          message: '删除文档失败：' + errMsg,
          collapsed: false,
          steps: this.deleteJobs[filename]?.steps || this.createDeleteSteps(),
        });
      }
    },

    startDeleteJobPolling(filename: string, jobId: string) {
      this.stopDeleteJobPolling(filename);

      const poll = async () => {
        try {
          const response = await api.get(`/documents/delete/jobs/${encodeURIComponent(jobId)}`);
          const job = response.data;
          this.syncDeleteJob(filename, job);

          if (job.status === 'completed') {
            this.stopDeleteJobPolling(filename);
            this.scheduleDeletedDocumentRemoval(filename);
          } else if (job.status === 'failed') {
            this.stopDeleteJobPolling(filename);
          }
        } catch (error: any) {
          const errMsg = error.response?.data?.detail || error.message || '查询失败';
          this.setDeleteJob(filename, {
            status: 'failed',
            message: '删除进度查询失败：' + errMsg,
            collapsed: false,
            steps: this.deleteJobs[filename]?.steps || this.createDeleteSteps(),
          });
          this.stopDeleteJobPolling(filename);
        }
      };

      poll();
      this.deletePollTimers = {
        ...this.deletePollTimers,
        [filename]: setInterval(poll, 1000),
      };
    },

    stopDeleteJobPolling(filename: string) {
      const timer = this.deletePollTimers[filename];
      if (!timer) return;
      clearInterval(timer);
      const { [filename]: _, ...rest } = this.deletePollTimers;
      this.deletePollTimers = rest;
    },

    stopAllDeleteJobPolling() {
      Object.keys(this.deletePollTimers).forEach((filename) => this.stopDeleteJobPolling(filename));
    },

    clearDeleteRemovalTimer(filename: string) {
      const timer = this.deleteRemoveTimers[filename];
      if (!timer) return;
      clearTimeout(timer);
      const { [filename]: _, ...rest } = this.deleteRemoveTimers;
      this.deleteRemoveTimers = rest;
    },

    scheduleDeletedDocumentRemoval(filename: string) {
      this.clearDeleteRemovalTimer(filename);
      const timer = setTimeout(async () => {
        this.documents = this.documents.filter((doc) => doc.filename !== filename);
        const { [filename]: _job, ...jobs } = this.deleteJobs;
        const { [filename]: _timer, ...timers } = this.deleteRemoveTimers;
        this.deleteJobs = jobs;
        this.deleteRemoveTimers = timers;
        await this.loadDocuments();
      }, 3000);
      this.deleteRemoveTimers = {
        ...this.deleteRemoveTimers,
        [filename]: timer,
      };
    },

    toggleDeleteJobCollapsed(filename: string) {
      const job = this.deleteJobs[filename];
      if (!job) return;
      this.setDeleteJob(filename, { collapsed: !job.collapsed });
    },
  },
});
export type { DocumentItem };
