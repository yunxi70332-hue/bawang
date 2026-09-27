import { defineStore } from 'pinia'
import { apiAuth } from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('token') || '',
    user: JSON.parse(localStorage.getItem('user') || 'null'),
    permissions: JSON.parse(localStorage.getItem('permissions') || '[]'),
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    displayName: (s) => s.user?.display_name || s.user?.username || '',
    roleName: (s) => s.user?.role?.name || '',
  },
  actions: {
    async login(payload) {
      const data = await apiAuth.login(payload)
      this.token = data.token
      this.user = data.user
      this.permissions = data.permissions
      localStorage.setItem('token', data.token)
      localStorage.setItem('user', JSON.stringify(data.user))
      localStorage.setItem('permissions', JSON.stringify(data.permissions))
    },
    async refresh() {
      if (!this.token) return
      try {
        this.user = await apiAuth.me()
        localStorage.setItem('user', JSON.stringify(this.user))
      } catch {
        /* 401 已由拦截器处理 */
      }
    },
    logout() {
      this.token = ''
      this.user = null
      this.permissions = []
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      localStorage.removeItem('permissions')
    },
    can(perm) {
      return this.permissions.includes(perm)
    },
  },
})
