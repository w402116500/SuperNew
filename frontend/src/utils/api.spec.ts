import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import api, { isPublicAuthRequest, shouldTreatAsExpiredSession } from './api';

describe('auth request classification', () => {
  it('treats login and register as public auth requests', () => {
    expect(isPublicAuthRequest('/auth/login')).toBe(true);
    expect(isPublicAuthRequest('/auth/register')).toBe(true);
    expect(isPublicAuthRequest('http://localhost:8050/auth/login')).toBe(true);
  });

  it('does not treat current-user checks as public auth requests', () => {
    expect(isPublicAuthRequest('/auth/me')).toBe(false);
    expect(isPublicAuthRequest('/documents')).toBe(false);
  });
});

describe('session expiration handling', () => {
  it('does not treat a wrong password as an expired session', () => {
    expect(
      shouldTreatAsExpiredSession({
        response: { status: 401 },
        config: { url: '/auth/login' },
      })
    ).toBe(false);
  });

  it('does not treat a register 401 as an expired session', () => {
    expect(
      shouldTreatAsExpiredSession({
        response: { status: 401 },
        config: { url: '/auth/register' },
      })
    ).toBe(false);
  });

  it('treats a 401 from a protected API as an expired session', () => {
    expect(
      shouldTreatAsExpiredSession({
        response: { status: 401 },
        config: { url: '/auth/me' },
      })
    ).toBe(true);
    expect(
      shouldTreatAsExpiredSession({
        response: { status: 401 },
        config: { url: '/documents' },
      })
    ).toBe(true);
  });

  it('ignores non-401 errors', () => {
    expect(
      shouldTreatAsExpiredSession({
        response: { status: 400 },
        config: { url: '/auth/login' },
      })
    ).toBe(false);
    expect(shouldTreatAsExpiredSession(null)).toBe(false);
  });
});

describe('api 401 interceptor', () => {
  const originalAdapter = api.defaults.adapter;
  const dispatchEvent = vi.fn();

  beforeEach(() => {
    dispatchEvent.mockReset();
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => 'old-token'),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    vi.stubGlobal('window', { dispatchEvent });
  });

  afterEach(() => {
    api.defaults.adapter = originalAdapter;
    vi.unstubAllGlobals();
  });

  const rejectWith401 = (url: string) => {
    api.defaults.adapter = async (config) => {
      const error = Object.assign(new Error('Request failed with status code 401'), {
        config: { ...config, url: config.url || url },
        response: { status: 401, data: { detail: '用户名或密码错误' } },
      });
      throw error;
    };
  };

  it('does not broadcast login expiry after a wrong password', async () => {
    rejectWith401('/auth/login');
    await expect(api.post('/auth/login', { username: 'alice', password: 'bad' })).rejects.toBeTruthy();
    expect(dispatchEvent).not.toHaveBeenCalled();
    expect(localStorage.removeItem).not.toHaveBeenCalled();
  });

  it('broadcasts login expiry for a protected 401', async () => {
    rejectWith401('/documents');
    await expect(api.get('/documents')).rejects.toBeTruthy();
    expect(dispatchEvent).toHaveBeenCalledTimes(1);
    expect(dispatchEvent.mock.calls[0][0]).toBeInstanceOf(Event);
    expect(dispatchEvent.mock.calls[0][0].type).toBe('unauthorized');
    expect(localStorage.removeItem).toHaveBeenCalledWith('accessToken');
  });
});
