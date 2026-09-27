import { useAuthStore } from '../stores/auth'

/** v-perm="'account:create'" —— 无权限时移除元素（配合路由级 meta.perm 双重控制）。 */
export const perm = {
  mounted(el, binding) {
    const auth = useAuthStore()
    const need = Array.isArray(binding.value) ? binding.value : [binding.value]
    if (need.length && !need.some((p) => auth.permissions.includes(p))) {
      el.parentNode?.removeChild(el)
    }
  },
}
