import axios from 'axios';

const api = axios.create({
  timeout: 60000,
});

export function isPublicAuthRequest(url: string | undefined): boolean {
  const path = String(url || '');
  return path.includes('/auth/login') || path.includes('/auth/register');
}

export function shouldTreatAsExpiredSession(error: {
  response?: { status?: number };
  config?: { url?: string };
} | null | undefined): boolean {
  if (error?.response?.status !== 401) {
    return false;
  }
  return !isPublicAuthRequest(error.config?.url);
}

api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('accessToken');
    if (token && !isPublicAuthRequest(config.url)) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

api.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    if (shouldTreatAsExpiredSession(error)) {
      localStorage.removeItem('accessToken');
      window.dispatchEvent(new CustomEvent('unauthorized'));
    }
    return Promise.reject(error);
  }
);

export default api;
