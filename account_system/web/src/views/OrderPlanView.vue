<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">下单方案 · 下单决策</h2>
        <p class="page-subtitle">
          方案 = 下单时的选券控制单元（策略 + 券优先级层级）；decide / create 指定方案后按其层级链选券，
          不选方案时系统按成本最优自动选券；套餐（可接单范围）由系统自动匹配，在「套餐库」页签维护
        </p>
      </div>
      <el-button v-if="pageTab === 'plans'" type="primary" :icon="Plus" @click="openCreate">新增方案</el-button>
      <el-button v-else type="primary" :icon="Plus" @click="openPktCreate">新增套餐</el-button>
    </div>

    <el-tabs v-model="pageTab">
      <!-- ================ 页签一：方案管理 ================ -->
      <el-tab-pane :label="`方案管理（${total}）`" name="plans">
        <div class="search-bar">
          <el-input v-model="query.keyword" placeholder="方案名称关键字" clearable style="width: 220px"
            :prefix-icon="Search" @keyup.enter="search" @clear="search" />
          <el-select v-model="query.enabled" placeholder="启用状态" clearable style="width: 130px" @change="search">
            <el-option label="启用" :value="true" />
            <el-option label="停用" :value="false" />
          </el-select>
          <el-button type="primary" :icon="Search" @click="search">查询</el-button>
          <el-button :icon="Refresh" circle @click="load" />
        </div>

        <el-table v-loading="loading" :data="items" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="name" label="名称" min-width="140">
            <template #default="{ row }">
              <span class="name-cell" :title="row.note">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="策略" width="96" align="center">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="strategyTag(row.strategy)">
                {{ strategyLabel(row.strategy) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="金额上限" width="96" align="center">
            <template #default="{ row }">
              <span v-if="row.max_pay_amount" class="mono">{{ row.max_pay_amount }} 元</span>
              <el-tag v-else size="small" type="danger" effect="plain">未配置</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="饮品信息" min-width="140">
            <template #default="{ row }">
              <span v-if="row.drink_info" :title="row.drink_info">{{ row.drink_info }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="优先级层数" width="96" align="center">
            <template #default="{ row }">
              <span v-if="row.priority_count" class="mono">{{ row.priority_count }}</span>
              <el-tag v-else size="small" effect="plain">纯策略</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="饮品白名单" width="96" align="center">
            <template #default="{ row }">
              <span v-if="(row.drinks || []).length" class="mono">{{ row.drinks.length }}</span>
              <el-tag v-else size="small" effect="plain">不限</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="80" align="center">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" :loading="row._toggling" @change="(v) => toggleEnabled(row, v)" />
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="165">
            <template #default="{ row }"><span class="mono">{{ fmtTime(row.updated_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{ row }">
              <el-button size="small" plain @click="openEdit(row)">编辑</el-button>
              <el-button size="small" type="danger" plain @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无下单方案：不建方案时系统按成本最优自动选券" :image-size="70" />
          </template>
        </el-table>
      </el-tab-pane>

      <!-- ================ 页签二：套餐库（2026-09-29 §13 并入方案体系） ================ -->
      <el-tab-pane :label="`套餐库（${pktTotal}）`" name="packets">
        <div class="search-bar">
          <el-input v-model="pktQuery.keyword" placeholder="套餐名称关键字" clearable style="width: 220px"
            :prefix-icon="Search" @keyup.enter="pktSearch" @clear="pktSearch" />
          <el-select v-model="pktQuery.open" placeholder="开放状态" clearable style="width: 130px" @change="pktSearch">
            <el-option label="开放" :value="true" />
            <el-option label="关闭" :value="false" />
          </el-select>
          <el-button type="primary" :icon="Search" @click="pktSearch">查询</el-button>
          <el-button :icon="Refresh" circle @click="loadPkt" />
        </div>

        <el-table v-loading="pktLoading" :data="pktItems" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="name" label="名称" min-width="150">
            <template #default="{ row }">
              <span class="name-cell" :title="row.note">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="价格区间" width="140">
            <template #default="{ row }">
              <span class="price">¥{{ pktPriceRange(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="开放状态" width="90" align="center">
            <template #default="{ row }">
              <el-switch :model-value="row.open_flag" :loading="row._toggling" @change="(v) => togglePktOpen(row, v)" />
            </template>
          </el-table-column>
          <el-table-column label="商品数" width="100" align="center">
            <template #default="{ row }">
              <el-tag v-if="!row.item_count" size="small" effect="plain">全品类</el-tag>
              <span v-else>{{ row.item_count }}</span>
            </template>
          </el-table-column>
          <el-table-column label="时段" min-width="120">
            <template #default="{ row }">
              <span v-if="row.available_start || row.available_end" class="mono">
                {{ row.available_start || '00:00:00' }} ~ {{ row.available_end || '23:59:59' }}
              </span>
              <span v-else class="muted">不限</span>
            </template>
          </el-table-column>
          <el-table-column label="最低利润" width="94" align="center">
            <template #default="{ row }">
              <span v-if="row.min_profit" class="price">¥{{ row.min_profit }}</span>
              <span v-else class="muted">全局</span>
            </template>
          </el-table-column>
          <el-table-column label="最大承受" width="94" align="center">
            <template #default="{ row }">
              <span v-if="row.max_order_cost" class="price">¥{{ row.max_order_cost }}</span>
              <span v-else class="muted">全局</span>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="165">
            <template #default="{ row }"><span class="mono">{{ fmtTime(row.updated_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{ row }">
              <el-button size="small" plain @click="openPktEdit(row)">编辑</el-button>
              <el-button size="small" type="danger" plain @click="removePkt(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="pager">
          <el-pagination background layout="total, prev, pager, next" :total="pktTotal"
            v-model:current-page="pktQuery.page" :page-size="pktQuery.page_size" @current-change="loadPkt" />
        </div>
        <p class="muted pkt-note">
          套餐 = 可接单范围（价格区间/时段/商品白名单/阈值覆盖），由 decide 按金额区间与时段自动匹配命中；
          方案与套餐相互独立——方案管选券策略，套餐管接单范围
        </p>
      </el-tab-pane>
    </el-tabs>

    <!-- ================= 方案 新增 / 编辑弹窗 ================= -->
    <el-dialog v-model="dlg" :title="editingId ? `编辑方案 #${editingId}` : '新增方案'" width="760px"
      destroy-on-close :close-on-click-modal="false">
      <el-tabs v-model="dlgTab">
        <!-- Tab 1：基础信息 -->
        <el-tab-pane label="基础信息" name="base">
          <el-form ref="formRef" :model="form" :rules="rules" label-width="120px">
            <el-form-item label="方案名称" prop="name">
              <el-input v-model="form.name" maxlength="64" placeholder="如：DN券优先方案" />
            </el-form-item>
            <el-form-item label="选券策略">
              <el-radio-group v-model="form.strategy" class="strategy-col">
                <el-radio v-for="s in PLAN_STRATEGIES" :key="s.value" :value="s.value">
                  {{ s.label }}<span class="muted strategy-hint">{{ s.hint }}</span>
                </el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="支付金额上限(元)" prop="max_pay_amount">
              <el-input v-model="form.max_pay_amount" placeholder="如 15.70，本方案下单实付金额不得超过该值" clearable />
              <div class="muted form-hint">安全限制：超过该金额的订单将被拒绝；未配置时后端默认拒绝交易</div>
            </el-form-item>
            <el-form-item label="饮品信息" prop="drink_info">
              <el-input v-model="form.drink_info" type="textarea" :rows="2" maxlength="200"
                show-word-limit clearable resize="none"
                placeholder="必填：饮品相关信息（如客户要求、口味备注、杯型说明等）；下单选此方案时自动带入订单，提交订单前强制非空" />
            </el-form-item>
            <el-form-item label="启用">
              <el-switch v-model="form.enabled" />
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="form.note" maxlength="255" placeholder="可选" />
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- Tab 2：优先级层级（券规则结构统一：匹配方式 + 匹配值 + 面额校验） -->
        <el-tab-pane :label="`优先级层级（${form.priorities.length}）`" name="tiers">
          <div class="items-head">
            <h3 class="sec-title">
              优先级层级
              <span class="muted sub">（{{ form.priorities.length }} 层；行序即优先级，第 1 行最优先；未匹配任何层的券按策略排序垫底）</span>
            </h3>
            <el-button type="primary" size="small" :icon="Plus" @click="addTier">添加优先级</el-button>
          </div>
          <el-table :data="form.priorities" size="small" border>
            <el-table-column label="层级" width="96">
              <template #default="{ $index }">
                <div class="tier-rank">
                  <el-tag size="small" effect="plain">第{{ $index + 1 }}优先</el-tag>
                  <span class="tier-move">
                    <el-button link size="small" :icon="ArrowUp" :disabled="$index === 0"
                      @click="moveTier($index, -1)" />
                    <el-button link size="small" :icon="ArrowDown" :disabled="$index === form.priorities.length - 1"
                      @click="moveTier($index, 1)" />
                  </span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="层名" width="126">
              <template #default="{ row, $index }">
                <el-input v-model="row.name" size="small" :placeholder="`如：20元DN券 · 留空=第${$index + 1}优先`" />
              </template>
            </el-table-column>
            <el-table-column label="优惠券绑定 → 匹配规则（绑定 + 匹配方式 + 匹配值 + 面额校验）" min-width="380">
              <template #default="{ row, $index }">
                <CouponTypeBindSelect class="tier-bind" :model-value="tierBindValue(row)"
                  @select="(t) => applyTierBinding(row, t)" @clear="clearTierBinding(row)" />
                <CouponRuleFields v-model="form.priorities[$index]" :clearable="false" />
                <div v-if="$index === 0 && !String(row.match_value).trim()" class="muted tier-hint">
                  如：绑定「霸王茶姬20元代金券-DN」或手填 券名包含「20元代金券-DN」
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="58" align="center">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" @click="removeTier($index)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="未设置层级：纯策略排序（系统自动）" :image-size="60" />
            </template>
          </el-table>
          <p class="muted tier-note">
            「优惠券绑定」下拉实时读券档案库（coupon_records，与「优惠券查询·功能4」全量查询落库同步），
            支持搜索与分页加载；选中券类型自动填充 匹配方式=券名精确 + 完整券名 + 面额校验，也可手填匹配规则；
            未填匹配值的空行保存时自动忽略，层级按行序重新编号（1..N）
          </p>
        </el-tab-pane>

        <!-- Tab 3：饮品管理（共享选品器：模糊搜索 + 多选关联，保存后方案仅可下单已关联饮品） -->
        <el-tab-pane :label="`饮品管理（${form.drinks.length}）`" name="drinks">
          <MenuSkuPicker :selected="selectedSkuSet" add-label="加入已选" @add="addSelectedDrinks">
            <template #bar>
              <span class="muted drink-count">已关联 {{ form.drinks.length }} 种饮品</span>
            </template>
          </MenuSkuPicker>

          <div class="items-head" style="margin-top: 12px">
            <h3 class="sec-title">
              已关联饮品
              <span class="muted sub">（{{ form.drinks.length }} 种；保存后该方案仅可下单这些饮品，留空 = 不限）</span>
            </h3>
          </div>
          <el-table :data="form.drinks" size="small" border max-height="220">
            <el-table-column prop="drink_name" label="饮品（含规格）" min-width="180" show-overflow-tooltip />
            <el-table-column label="面价" width="76">
              <template #default="{ row }">¥{{ row.face_price || '—' }}</template>
            </el-table-column>
            <el-table-column prop="spu_id" label="SPU" width="130" show-overflow-tooltip />
            <el-table-column prop="sku_id" label="SKU" width="150" show-overflow-tooltip />
            <el-table-column label="操作" width="64" align="center">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" @click="form.drinks.splice($index, 1)">移除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="未关联饮品：该方案不限制可点饮品" :image-size="60" />
            </template>
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- ================= 套餐 新增 / 编辑弹窗（自原套餐配置页整体迁入） ================= -->
    <el-dialog v-model="pktDlg" :title="pktEditingId ? `编辑套餐 #${pktEditingId}` : '新增套餐'" width="1080px"
      append-to-body destroy-on-close :close-on-click-modal="false">
      <el-tabs v-model="pktDlgTab">
        <!-- Tab 1：基础信息 -->
        <el-tab-pane label="基础信息" name="base">
          <el-form ref="pktFormRef" :model="pktForm" :rules="pktRules" label-width="108px">
            <el-row :gutter="12">
              <el-col :span="8">
                <el-form-item label="套餐名称" prop="name">
                  <el-input v-model="pktForm.name" maxlength="64" placeholder="如：伯牙绝弦套餐" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="最小金额">
                  <el-input v-model="pktForm.min_order_amount" placeholder="客户支付价下限，默认 0" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="最大金额">
                  <el-input v-model="pktForm.max_order_amount" placeholder="0 = 不设上限" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="时段开始">
                  <el-time-select v-model="pktForm.available_start" start="00:00" end="23:59" step="00:15"
                    placeholder="不限" clearable style="width: 100%" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="时段结束">
                  <el-time-select v-model="pktForm.available_end" start="00:00" end="23:59" step="00:15"
                    placeholder="不限" clearable style="width: 100%" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="最低利润">
                  <el-input v-model="pktForm.min_profit" placeholder="元，留空 = 用全局配置" />
                </el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="最大承受金额">
                  <el-input v-model="pktForm.max_order_cost" placeholder="元，留空 = 用全局" />
                </el-form-item>
              </el-col>
              <el-col :span="24">
                <el-form-item label="备注">
                  <el-input v-model="pktForm.note" maxlength="255" placeholder="可选" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
        </el-tab-pane>

        <!-- Tab 2：商品与券规则（共享选品器 + 商品清单） -->
        <el-tab-pane :label="`商品与券规则（${pktForm.items.length}）`" name="items">
          <div class="items-head">
            <h3 class="sec-title">
              商品清单
              <span class="muted sub">（{{ pktForm.items.length }} 项，留空 = 全品类可命中；每行可设普通券 / 溢价券匹配规则，展开行编辑溢价券规则）</span>
            </h3>
          </div>
          <MenuSkuPicker :selected="selectedItemSkus"
            placeholder="输入商品关键词搜索（如：伯牙绝弦 / 桂花）" add-label="批量加入清单"
            @add="addItems" />
          <el-table :data="pktForm.items" size="small" border max-height="360">
            <el-table-column type="expand" width="36">
              <template #default="{ row }">
                <div class="rule-expand">
                  <span class="rule-label">溢价券规则</span>
                  <CouponRuleFields v-model="row.premium_rule" compact class="rule-expand-fields" />
                  <span class="muted rule-tip">仅当该商品需溢价券（右侧开关）时生效</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="商品（SPU / SKU）" min-width="220">
              <template #default="{ row }">
                <div class="prod-cell">
                  <div class="prod-name">{{ row.product_name || '（未选择）' }}</div>
                  <div class="mono muted prod-ids">SPU {{ row.spu_id || '—' }} · SKU {{ row.sku_id || '—' }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="面价" width="110">
              <template #default="{ row }">
                <el-input v-model="row.face_price" size="small" placeholder="面价" />
              </template>
            </el-table-column>
            <el-table-column label="溢价" width="110">
              <template #default="{ row }">
                <el-input v-model="row.premium_price" size="small" placeholder="可空" />
              </template>
            </el-table-column>
            <el-table-column label="溢价券商品" width="90" align="center">
              <template #default="{ row }">
                <el-switch v-model="row.is_premium" />
              </template>
            </el-table-column>
            <el-table-column label="普通券规则（匹配方式 + 匹配值 + 面额）" min-width="330">
              <template #default="{ row }">
                <CouponRuleFields v-model="row.normal_rule" />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="70" align="center">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" @click="pktForm.items.splice($index, 1)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="未限定商品（全品类）；在上方搜索框从菜单库批量添加白名单商品" :image-size="60" />
            </template>
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <template #footer>
        <el-button @click="pktDlg = false">取消</el-button>
        <el-button type="primary" :loading="pktSaving" @click="savePkt">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowUp, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { apiDecision } from '../api'
import { fmtTime } from '../utils/format'
import { PLAN_STRATEGIES, strategyLabel, strategyTag } from '../constants/decision'
import CouponRuleFields from '../components/CouponRuleFields.vue'
import CouponTypeBindSelect from '../components/CouponTypeBindSelect.vue'
import MenuSkuPicker from '../components/MenuSkuPicker.vue'

const pageTab = ref('plans')

/* ==================== 页签一：方案管理 ==================== */
const loading = ref(false)
const items = ref([])
const total = ref(0)
const query = reactive({ keyword: '', enabled: '' })

async function load() {
  loading.value = true
  try {
    const params = { keyword: query.keyword.trim() }
    if (query.enabled === true || query.enabled === false) params.enabled = query.enabled
    const data = await apiDecision.orderPlans({ params })
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function search() {
  load()
}

/* 行内启用开关：独立 toggle 端点仅翻转 enabled，层级/饮品关联原样保留 */
async function toggleEnabled(row, v) {
  row._toggling = true
  try {
    const res = await apiDecision.orderPlanToggleEnabled(row.id)
    Object.assign(row, res)   // 返回 _plan_detail，原地刷新
    ElMessage.success(res.enabled ? '方案已启用' : '方案已停用')
  } catch {
    /* 失败保持原值 */
  } finally {
    row._toggling = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除下单方案「${row.name}」？其优先级层级与饮品关联将一并删除。`, '删除确认', { type: 'warning' })
    .catch(() => Promise.reject())
  await apiDecision.orderPlanDelete(row.id)
  ElMessage.success('已删除')
  load()
}

/* ---------------- 方案 新增 / 编辑 ---------------- */
const dlg = ref(false)
const saving = ref(false)
const formRef = ref()
const editingId = ref(null)
const dlgTab = ref('base')
const form = reactive({
  name: '', strategy: 'cost_first', drink_info: '', enabled: true, note: '',
  max_pay_amount: '',   // 支付金额上限（元）：新增默认空串，靠必填校验强制填写
  priorities: [], drinks: [],
})
const rules = {
  name: [{ required: true, message: '请输入方案名称', trigger: 'blur' }],
  drink_info: [{ required: true, message: '饮品信息为必填项，请填写饮品相关信息后再保存方案', trigger: 'blur' }],
  max_pay_amount: [
    { required: true, message: '支付金额上限须为非负金额（如 15.70）', trigger: 'blur' },
    { pattern: /^\d+(\.\d{1,2})?$/, message: '支付金额上限须为非负金额（如 15.70）', trigger: 'blur' },
  ],
}

const selectedSkuSet = computed(() => new Set(form.drinks.map((d) => d.sku_id)))

function newTierRow() {
  return { name: '', match_type: 'template_contains', match_value: '', face_value: '' }
}

function openCreate() {
  editingId.value = null
  dlgTab.value = 'base'
  Object.assign(form, {
    name: '', strategy: 'cost_first', drink_info: '', enabled: true, note: '',
    max_pay_amount: '',
    priorities: [newTierRow(), newTierRow()],   // 两行示例引导（首行提示典型用法）
    drinks: [],
  })
  dlg.value = true
}

function openEdit(row) {
  editingId.value = row.id
  dlgTab.value = 'base'
  Object.assign(form, {
    name: row.name,
    strategy: row.strategy || 'cost_first',
    drink_info: row.drink_info || '',
    enabled: !!row.enabled,
    note: row.note || '',
    max_pay_amount: row.max_pay_amount ? String(row.max_pay_amount) : '',   // 旧方案未配置时回空串，保存时强制补填
    drinks: (row.drinks || []).map((d) => ({
      spu_id: d.spu_id || '', sku_id: d.sku_id,
      drink_name: d.drink_name || '', face_price: d.face_price || '',
    })),
    priorities: (row.priorities || []).map((p) => ({
      name: p.name || '',
      match_type: p.match_type || 'template_contains',
      match_value: p.match_value || '',
      face_value: p.face_value || '',
    })),
  })
  dlg.value = true
}

async function save() {
  try {
    await formRef.value.validate()
  } catch (e) {
    dlgTab.value = 'base'   // 校验失败切回基础信息 Tab，让必填项错误可见
    return Promise.reject(e)
  }
  const payload = {
    name: form.name.trim(),
    strategy: form.strategy,
    max_pay_amount: form.max_pay_amount.trim(),   // 必填：未配置时后端拒绝该方案下单
    drink_info: form.drink_info.trim(),
    note: form.note,
    enabled: form.enabled,
    drinks: form.drinks.map((d) => ({
      spu_id: d.spu_id, sku_id: d.sku_id,
      drink_name: d.drink_name, face_price: d.face_price,
    })),
    priorities: form.priorities
      .filter((p) => p.match_type && String(p.match_value).trim())   // 未填匹配值的空行不下发
      .map((p, i) => ({
        level: i + 1,                                   // 按行序重编 level（1..N）
        name: String(p.name).trim() || `第${i + 1}优先`,
        match_type: p.match_type,
        match_value: String(p.match_value).trim(),
        face_value: String(p.face_value).trim(),
      })),
  }
  saving.value = true
  try {
    if (editingId.value) {
      await apiDecision.orderPlanUpdate(editingId.value, payload)
      ElMessage.success('方案已保存')
    } else {
      await apiDecision.orderPlanCreate(payload)
      ElMessage.success('方案已创建')
    }
    dlg.value = false
    load()
  } finally {
    saving.value = false
  }
}

/* ---------------- 优先级层级编辑 ---------------- */
function addTier() {
  form.priorities.push(newTierRow())
}

function removeTier(index) {
  form.priorities.splice(index, 1)
}

function moveTier(index, dir) {
  const target = index + dir
  if (target < 0 || target >= form.priorities.length) return
  const arr = form.priorities
  ;[arr[index], arr[target]] = [arr[target], arr[index]]
}

/* ---------------- 优惠券绑定：券类型下拉选中 → 回填层级行规则字段 ---------------- */
/* 名称类匹配（券名精确/包含）才镜像到绑定下拉显示；正则/券码前缀属手填规则不回显 */
const NAME_MATCH_TYPES = new Set(['template_exact', 'template_contains'])

function tierBindValue(tier) {
  return NAME_MATCH_TYPES.has(tier.match_type) ? String(tier.match_value || '') : ''
}

/* 选中券类型：匹配方式=券名精确 + 完整模板名（档案 template_name，与 rule_satisfied
   的 template_exact 全等语义对齐）；面额校验取档案面额（折扣/兑换券无面额不动）；
   层名留空时以权益文案兜底（如「20元代金」） */
function applyTierBinding(tier, t) {
  tier.match_type = 'template_exact'
  tier.match_value = t.template_name
  const amount = String(t.amount || '').trim()
  if (amount) tier.face_value = amount
  if (!String(tier.name || '').trim()) {
    tier.name = String(t.benefit_text || '').trim() || String(t.template_name).slice(0, 32)
  }
}

/* 清空绑定：仅清匹配值与面额（匹配方式保留，行仍在保存时因匹配值为空被忽略） */
function clearTierBinding(tier) {
  tier.match_value = ''
  tier.face_value = ''
}

/* ---------------- 饮品管理：共享选品器批量加入 ---------------- */
function addSelectedDrinks(rows) {
  const fresh = []
  for (const row of rows) {
    if (selectedSkuSet.value.has(row.sku_id)) continue
    form.drinks.push({
      spu_id: String(row.spu_id || ''), sku_id: String(row.sku_id),
      drink_name: `${row.spu_name}${row.spec_desc && row.spec_desc !== '默认' ? `（${row.spec_desc}）` : ''}`,
      face_price: String(row.price || ''),
    })
    fresh.push(row.spu_name)
  }
  if (fresh.length) {
    ElMessage.success(`已加入 ${fresh.length} 种饮品：${fresh.slice(0, 3).join('、')}${fresh.length > 3 ? ' 等' : ''}`)
  } else {
    ElMessage.info('所选饮品均已关联')
  }
}

/* ==================== 页签二：套餐库（自原套餐配置页迁入，§13） ==================== */
const pktLoading = ref(false)
const pktItems = ref([])
const pktTotal = ref(0)
const pktQuery = reactive({ keyword: '', open: '', page: 1, page_size: 20 })

async function loadPkt() {
  pktLoading.value = true
  try {
    const params = { keyword: pktQuery.keyword.trim(), page: pktQuery.page, page_size: pktQuery.page_size }
    if (pktQuery.open === true || pktQuery.open === false) params.open = pktQuery.open
    const data = await apiDecision.packets(params)
    pktItems.value = data.items
    pktTotal.value = data.total
  } finally {
    pktLoading.value = false
  }
}

function pktSearch() {
  pktQuery.page = 1
  loadPkt()
}

function pktPriceRange(row) {
  const min = row.min_order_amount || '0'
  const max = Number(row.max_order_amount) > 0 ? row.max_order_amount : '不限'
  return `${min} - ${max}`
}

async function togglePktOpen(row, v) {
  row._toggling = true
  try {
    const res = await apiDecision.packetToggleOpen(row.id)
    Object.assign(row, res)
    ElMessage.success(res.open_flag ? '套餐已开放' : '套餐已关闭')
  } catch {
    /* 失败保持原值 */
  } finally {
    row._toggling = false
  }
}

async function removePkt(row) {
  await ElMessageBox.confirm(`确定删除套餐「${row.name}」？其商品清单将一并删除。`,
    '删除确认', { type: 'warning' })
    .catch(() => Promise.reject())
  await apiDecision.packetDelete(row.id)
  ElMessage.success('已删除')
  loadPkt()
}

/* ---------------- 套餐 新增 / 编辑 ---------------- */
const pktDlg = ref(false)
const pktSaving = ref(false)
const pktFormRef = ref()
const pktEditingId = ref(null)
const pktDlgTab = ref('base')
const pktForm = reactive({
  name: '', min_order_amount: '0', max_order_amount: '0',
  available_start: '', available_end: '', min_profit: '', max_order_cost: '', note: '', items: [],
})
const pktRules = {
  name: [{ required: true, message: '请输入套餐名称', trigger: 'blur' }],
}

const selectedItemSkus = computed(() => pktForm.items.map((it) => it.sku_id))

function blankRule() {
  return { match_type: '', match_value: '', face_value: '' }
}

function newItemRow(base = {}) {
  return {
    spu_id: base.spu_id || '',
    sku_id: base.sku_id || '',
    product_name: base.product_name || '',
    face_price: base.face_price || '',
    premium_price: base.premium_price || '',
    is_premium: !!base.is_premium,
    normal_rule: { ...blankRule(), ...(base.normal_coupon_rule || {}) },
    premium_rule: { ...blankRule(), ...(base.premium_coupon_rule || {}) },
  }
}

/* 表单内编辑态 rule → 契约 JSON：match_type 与 match_value 均非空才下发（face_value 可选附加） */
function rulePayload(rule) {
  return rule?.match_type && rule?.match_value
    ? {
        match_type: rule.match_type,
        match_value: rule.match_value,
        ...(String(rule.face_value || '').trim() ? { face_value: String(rule.face_value).trim() } : {}),
      }
    : null
}

function openPktCreate() {
  pktEditingId.value = null
  pktDlgTab.value = 'base'
  Object.assign(pktForm, {
    name: '', min_order_amount: '0', max_order_amount: '0',
    available_start: '', available_end: '', min_profit: '', max_order_cost: '', note: '', items: [],
  })
  pktDlg.value = true
}

async function openPktEdit(row) {
  pktEditingId.value = row.id
  pktDlgTab.value = 'base'
  const detail = await apiDecision.packetGet(row.id)
  Object.assign(pktForm, {
    name: detail.name,
    min_order_amount: detail.min_order_amount ?? '0',
    max_order_amount: detail.max_order_amount ?? '0',
    // 后端 "HH:MM:SS" → el-time-select "HH:MM"
    available_start: detail.available_start ? String(detail.available_start).slice(0, 5) : '',
    available_end: detail.available_end ? String(detail.available_end).slice(0, 5) : '',
    min_profit: detail.min_profit || '',
    max_order_cost: detail.max_order_cost || '',
    note: detail.note || '',
    items: (detail.items || []).map(newItemRow),
  })
  pktDlg.value = true
}

async function savePkt() {
  try {
    await pktFormRef.value.validate()
  } catch (e) {
    pktDlgTab.value = 'base'
    return Promise.reject(e)
  }
  const payload = {
    name: pktForm.name.trim(),
    min_order_amount: String(pktForm.min_order_amount).trim() || '0',
    max_order_amount: String(pktForm.max_order_amount).trim() || '0',
    available_start: pktForm.available_start ? `${pktForm.available_start}:00` : '',
    available_end: pktForm.available_end ? `${pktForm.available_end}:00` : '',
    min_profit: String(pktForm.min_profit).trim(),
    max_order_cost: String(pktForm.max_order_cost).trim(),
    note: pktForm.note,
    items: pktForm.items
      .filter((it) => it.sku_id)
      .map((it) => ({
        spu_id: it.spu_id,
        sku_id: it.sku_id,
        product_name: it.product_name,
        face_price: it.face_price,
        premium_price: it.premium_price,
        is_premium: it.is_premium,
        normal_coupon_rule: rulePayload(it.normal_rule),
        premium_coupon_rule: rulePayload(it.premium_rule),
      })),
  }
  pktSaving.value = true
  try {
    if (pktEditingId.value) {
      await apiDecision.packetUpdate(pktEditingId.value, payload)
      ElMessage.success('套餐已保存')
    } else {
      await apiDecision.packetCreate(payload)
      ElMessage.success('套餐已创建')
    }
    pktDlg.value = false
    loadPkt()
  } finally {
    pktSaving.value = false
  }
}

/* 套餐商品批量加入：按 sku 去重，名称/面价按「名称（规格）」/菜单价回填 */
function addItems(rows) {
  const have = new Set(selectedItemSkus.value)
  const fresh = []
  for (const row of rows) {
    if (have.has(row.sku_id)) continue
    have.add(row.sku_id)
    pktForm.items.push(newItemRow({
      spu_id: String(row.spu_id || ''),
      sku_id: String(row.sku_id),
      product_name: `${row.spu_name}${row.spec_desc && row.spec_desc !== '默认' ? `（${row.spec_desc}）` : ''}`,
      face_price: String(row.price || ''),
    }))
    fresh.push(row.spu_name)
  }
  if (fresh.length) {
    ElMessage.success(`已加入 ${fresh.length} 种商品：${fresh.slice(0, 3).join('、')}${fresh.length > 3 ? ' 等' : ''}`)
  } else {
    ElMessage.info('所选商品均已在清单中')
  }
}

onMounted(() => {
  load()
  loadPkt()
})
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.name-cell {
  word-break: break-all;
}
.muted {
  color: var(--muted);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
.pkt-note {
  font-size: 12.5px;
  line-height: 1.6;
  margin: 10px 0 0;
}
.sec-title {
  margin: 0;
  font-size: 15px;
  color: var(--tea-800);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.sub {
  font-size: 12.5px;
  font-weight: 400;
}
.items-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 6px 0 10px;
  padding-top: 10px;
  border-top: 1px dashed #e3eae6;
}
.drink-count {
  font-size: 12.5px;
}
.price {
  color: var(--el-color-danger);
  font-weight: 600;
}
.strategy-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
}
.strategy-col .el-radio {
  margin-right: 0;
}
.strategy-hint {
  font-size: 12px;
  margin-left: 6px;
  font-weight: 400;
}
/* 表单项下方 muted 小字说明（支付金额上限安全限制提示等） */
.form-hint {
  font-size: 12px;
  line-height: 1.6;
  margin-top: 2px;
}
.tier-rank {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: flex-start;
}
.tier-move {
  display: flex;
  gap: 2px;
}
.tier-bind {
  width: 100%;
  margin-bottom: 6px;
}
.tier-hint {
  font-size: 12px;
  margin-top: 3px;
}
.tier-note {
  font-size: 12px;
  line-height: 1.6;
  margin: 8px 0 0;
}
.prod-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: flex-start;
}
.prod-name {
  font-weight: 600;
  color: var(--tea-800);
}
.prod-ids {
  font-size: 11.5px;
}
.rule-expand {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  padding: 6px 12px;
}
.rule-expand-fields {
  flex: 1;
  min-width: 380px;
}
.rule-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--tea-800);
}
.rule-tip {
  font-size: 12px;
}
</style>
