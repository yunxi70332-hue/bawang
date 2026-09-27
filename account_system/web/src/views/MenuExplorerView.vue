<template>
  <div>
    <div class="page-card">
      <h2 class="page-title">游客菜单浏览器 · 功能3</h2>
      <p class="page-subtitle">
        游客模式（无登录态）查询城市信息与门店菜品 SKU 配置，菜单链全程携带门店自取参数族（saleType=1 / saleChannel=2）
      </p>

      <div class="cascader">
        <el-select v-model="cityCode" filterable placeholder="① 选择城市" :loading="loadingCities"
          style="width: 200px" @change="onCityChange">
          <el-option v-for="c in cities" :key="c.cityCode" :label="c.cityName" :value="c.cityCode">
            <span>{{ c.cityName }}</span>
            <span class="opt-sub">{{ c.provinceName }}</span>
          </el-option>
        </el-select>

        <el-select v-model="storeNo" filterable remote :remote-method="searchStores"
          :loading="loadingStores" placeholder="② 选择门店（可输入名称搜索）" style="width: 320px"
          @change="loadMenu" @focus="searchStores('')">
          <el-option v-for="s in stores" :key="s.storeNo" :label="s.storeName" :value="s.storeNo">
            <span>{{ s.storeName }}</span>
            <span class="opt-sub">{{ s.storeNo }} · {{ s.address || s.cityName || '' }}</span>
          </el-option>
        </el-select>

        <el-tag v-if="menuMeta" type="success" effect="plain" class="menu-meta">
          {{ menuMeta.categories }} 分类 · {{ menuMeta.items.length }} SPU · 自取 saleType=1
        </el-tag>
      </div>

      <el-alert v-if="pickupNote" type="info" :closable="false" class="pickup-note" :title="pickupNote" />

      <el-table v-loading="loadingMenu" :data="pagedMenu" stripe @row-click="openGoods">
        <el-table-column label="图片" width="76">
          <template #default="{ row }">
            <el-image v-if="row.img" :src="row.img" :preview-src-list="[row.img]"
              preview-teleported fit="cover"
              style="width: 48px; height: 48px; border-radius: 8px; cursor: pointer" />
            <div v-else class="img-fallback"><el-icon><Picture /></el-icon></div>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.categoryName }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="spuName" label="商品（点击查看 SKU，点图片放大）" min-width="230" />
        <el-table-column label="价格" width="95">
          <template #default="{ row }">
            <span v-if="row.price != null" class="price">¥{{ row.price }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="85">
          <template #default="{ row }">
            <el-tag v-if="row.saleOut" type="danger" size="small" effect="plain">售罄</el-tag>
            <el-tag v-else type="success" size="small" effect="plain">在售</el-tag>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="storeNo ? '该门店暂无菜单数据' : '请先选择城市与门店'" />
        </template>
      </el-table>

      <div v-if="menuMeta && menuMeta.items.length > pageSize" class="pager">
        <el-pagination background layout="prev, pager, next" :total="menuMeta.items.length"
          v-model:current-page="page" :page-size="pageSize" />
      </div>
    </div>

    <el-drawer v-model="dlgGoods" :title="goods.spuName || '商品 SKU'" size="520px">
      <div v-if="goods.img" class="goods-hero">
        <el-image :src="goods.img" :preview-src-list="[goods.img, ...goods.detailImages]"
          preview-teleported fit="cover" class="goods-hero-img" />
      </div>
      <el-alert v-if="goods.description" :title="goods.description" type="info" :closable="false" class="goods-desc" />
      <el-table :data="goods.skus" stripe size="small">
        <el-table-column label="规格" min-width="200">
          <template #default="{ row }">{{ row.specDesc || '默认' }}</template>
        </el-table-column>
        <el-table-column label="价格" width="90">
          <template #default="{ row }"><span class="price">¥{{ row.price }}</span></template>
        </el-table-column>
        <el-table-column label="库存" width="90">
          <template #default="{ row }">
            <el-tag :type="row.stock > 0 ? 'success' : 'danger'" size="small" effect="plain">
              {{ row.stock > 0 ? row.stock : '售罄' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="SKU ID" min-width="170">
          <template #default="{ row }"><span class="mono">{{ row.skuId }}</span></template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiOps } from '../api'

const cities = ref([])
const stores = ref([])
const cityCode = ref('')
const storeNo = ref('')
const loadingCities = ref(false)
const loadingStores = ref(false)
const loadingMenu = ref(false)
const menuMeta = ref(null)
const page = ref(1)
const pageSize = 20
const pickupNote = ref('')
const dlgGoods = ref(false)
const goods = ref({ skus: [] })

const pagedMenu = computed(() => {
  const items = menuMeta.value?.items || []
  return items.slice((page.value - 1) * pageSize, page.value * pageSize)
})

onMounted(async () => {
  loadingCities.value = true
  try {
    const data = await apiOps.cities()
    cities.value = data.cities
    const ctx = await apiOps.pickupContext()
    pickupNote.value = `自取路径参数族：${Object.entries(ctx.params).map(([k, v]) => `${k}=${v}`).join('；')}`
  } finally {
    loadingCities.value = false
  }
})

async function onCityChange() {
  storeNo.value = ''
  menuMeta.value = null
  await searchStores('')
}

async function searchStores(kw) {
  if (!cityCode.value) {
    ElMessage.warning('请先选择城市')
    return
  }
  loadingStores.value = true
  try {
    const data = await apiOps.stores({ city: cityCode.value, keyword: kw, page: 1, page_size: 100 })
    stores.value = data.items
  } finally {
    loadingStores.value = false
  }
}

async function loadMenu() {
  if (!storeNo.value) return
  loadingMenu.value = true
  page.value = 1
  try {
    const data = await apiOps.menu(storeNo.value)
    data.items.sort((a, b) => Number(a.saleOut) - Number(b.saleOut)) // 在售靠前
    menuMeta.value = data
  } finally {
    loadingMenu.value = false
  }
}

async function openGoods(row) {
  dlgGoods.value = true
  goods.value = { spuName: row.spuName, img: row.img || '', description: '', skus: [], detailImages: [] }
  try {
    goods.value = await apiOps.goods(row.spuId, storeNo.value)
  } catch {
    dlgGoods.value = false
  }
}
</script>

<style scoped>
.cascader {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.opt-sub {
  float: right;
  color: var(--muted);
  font-size: 12px;
  margin-left: 14px;
}
.menu-meta {
  font-size: 13px;
}
.pickup-note {
  margin-bottom: 14px;
}
.price {
  color: #c45656;
  font-weight: 600;
}
.pager {
  display: flex;
  justify-content: center;
  margin-top: 14px;
}
.goods-desc {
  margin-bottom: 14px;
}
.img-fallback {
  width: 48px;
  height: 48px;
  border-radius: 8px;
  background: var(--tea-100);
  color: var(--tea-300);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
}
.goods-hero {
  margin-bottom: 14px;
}
.goods-hero-img {
  width: 100%;
  height: 220px;
  border-radius: 12px;
}
</style>
