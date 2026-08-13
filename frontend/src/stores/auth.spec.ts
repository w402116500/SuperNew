import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import api from '@/utils/api';
import { useAuthStore } from './auth';

vi.mock('@/utils/api', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

describe('auth store input normalization', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
    setActivePinia(createPinia());
  });

  it('trims the username but preserves password whitespace', async () => {
    vi.mocked(api.post).mockResolvedValue({
      data: { access_token: 'token', username: 'alice', role: 'user' },
    });
    const store = useAuthStore();
    store.authForm.username = ' alice ';
    store.authForm.password = ' secret ';

    await store.handleAuthSubmit();

    expect(api.post).toHaveBeenCalledWith('/auth/login', {
      username: 'alice',
      password: ' secret ',
    });
  });

  it('rejects a whitespace-only password before the request', async () => {
    const store = useAuthStore();
    store.authForm.username = 'alice';
    store.authForm.password = '   ';

    await expect(store.handleAuthSubmit()).rejects.toThrow('用户名和密码不能为空');
    expect(api.post).not.toHaveBeenCalled();
  });
});
