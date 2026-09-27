<template>
  <div>
    <!-- 步骤条 -->
    <div class="page-card">
      <h2 class="page-title">下单工作台 · F5</h2>
      <p class="page-subtitle">
        账号（仅在线）→ 城市/门店/SKU → 试算草稿与选券 → 确认下单；试算草稿与支付窗口均 10 分钟有效，
        金额展示以服务端结算为准（纯协议链路，不含支付宝侧扣款动作）
      </p>
      <el-steps class="wb-steps" :active="step" :direction="narrow ? 'vertical' : 'horizontal'"
        align-center finish-status="success">
        <el-step title="① 选账号" description="仅在线账号可下单" />
        <el-step title="② 选门店与商品" description="城市 → 门店 → SKU" />
        <el-step title="③ 试算与选券" description="草稿 10 分钟有效" />
        <el-step title="④ 确认下单" description="二次确认后提交" />
      </el-steps>
    </div>

    <!-- 主流程体 -->
    <div v-if="!result" class="page-card wb-body">

      <!-- 步骤1：选账号 -->
      <template v-if="step === 0">
        <h3 class="sec-title">选择下单账号</h3>
        <template v-if="accounts.length">
          <div class="acc-bar">
            <el-select v-model="accountId" filterable placeholder="选择茶姬账号（仅「在线」状态可选）" style="width: 360px">
              <el-option v-for="a in accounts" :key="a.id" :value="a.id" :disabled="a.status !== 'online'"
                :label="`${a.label}（${a.phone_masked}${a.status === 'online' ? ' · 在线' : ' · ' + a.status_label + ' 不可用'}）`">
                <span>{{ a.label }}</span>
                <span class="opt-sub">
                  {{ a.phone_masked }} · {{ a.status_label }}{{ a.status !== 'online' ? '（不可下单）' : '' }}
                </span>
              </el-option>
            </el-select>
            <el-button type="primary" :disabled="!accountId" @click="step = 1">下一步 · 选门店与商品</el-button>
            <el-button v-if="!hasOnline" type="primary" plain @click="router.push('/accounts')">前往账号管理</el-button>
          </div>
          <el-alert v-if="!hasOnline" type="warning" :closable="false" class="acc-tip"
            title="暂无「在线」账号：下单依赖登录态协议链，请先到「账号管理」完成协议登录后再回来。" />
        </template>
        <el-empty v-else description="暂无账号：请先到「账号管理」页创建账号并完成协议登录">
          <el-button type="primary" plain @click="router.push('/accounts')">前往账号管理</el-button>
        </el-empty>
      </template>

      <!-- 步骤2：选门店与商品 -->
      <template v-else-if="step === 1">
        <div class="step-head">
          <h3 class="sec-title">选择门店与商品</h3>
          <div class="head-right">
            <el-tag effect="plain" type="success">下单账号：{{ accountLabel }}</el-tag>
            <el-button link type="primary" @click="backToAccount">切换账号</el-button>
          </div>
        </div>

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
            @change="onStoreChange" @focus="searchStores('')">
            <el-option v-for="s in stores" :key="s.storeNo" :label="s.storeName" :value="s.storeNo">
              <span>{{ s.storeName }}</span>
              <span class="opt-sub">{{ s.storeNo }} · {{ s.address || s.cityName || '' }}</span>
            </el-option>
          </el-select>

          <el-tag v-if="menuMeta" type="success" effect="plain" class="menu-meta">
            {{ menuMeta.categories }} 分类 · {{ menuMeta.items.length }} SPU · 自取 saleType=1
          </el-tag>
        </div>

        <el-table v-loading="loadingMenu" :data="pagedMenu" stripe class="wb-menu"
          :row-class-name="rowClassName" @row-click="openGoods">
          <el-table-column label="图片" width="76">
            <template #default="{ row }">
              <el-image v-if="row.img" :src="row.img" :preview-src-list="[row.img]" preview-teleported
                fit="cover" style="width: 48px; height: 48px; border-radius: 8px; cursor: pointer"
                @click.stop />
              <div v-else class="img-fallback"><el-icon><Picture /></el-icon></div>
            </template>
          </el-table-column>
          <el-table-column label="分类" width="110">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.categoryName }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="spuName" label="商品（点击选择 SKU，点图片放大）" min-width="230" />
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
      </template>

      <!-- 步骤3：试算与选券 -->
      <template v-else-if="step === 2 && settleData">
        <div class="step-head">
          <h3 class="sec-title">试算预览与选券</h3>
          <el-tag :type="draftExpired ? 'danger' : 'info'" effect="plain">
            草稿{{ draftExpired ? '已过期' : ` ${fmtCountdown(draftLeft)} 后过期` }}
          </el-tag>
        </div>

        <el-alert v-if="draftExpired" type="error" :closable="false" class="block-gap"
          title="试算草稿已过期（10 分钟），请返回重新选择商品试算。" />

        <div class="preview-grid">
          <!-- 商品明细 -->
          <div class="preview-col">
            <div class="pv-row" v-for="(g, i) in settleData.preview.goods" :key="i">
              <span class="pv-name">{{ g.name }} <span class="muted">× {{ g.quantity }}</span></span>
              <span class="price">¥{{ g.price }}</span>
            </div>
            <div class="pv-row pv-total">
              <span>总额</span>
              <span class="price">¥{{ settleData.preview.total_trade_price }}</span>
            </div>
          </div>

          <!-- 券与应付：卡片式选券（完整名称/面额/门槛/有效期/可用性校验/预计抵扣） -->
          <div class="preview-col">
            <div class="pv-label coupon-head">
              选择优惠券
              <span class="muted">（{{ couponOptions.length }} 张，{{ couponOptions.length - disabledCount }} 张可用）</span>
            </div>
            <div class="coupon-list">
              <div class="coupon-item" :class="{ active: !selectedCouponCode }" @click="pickCoupon(null)">
                <el-radio :model-value="selectedCouponCode || 'none'" value="none">不使用优惠券</el-radio>
                <span class="muted coupon-sub">按原价结算</span>
              </div>
              <div v-for="c in couponOptions" :key="c.couponCode" class="coupon-item"
                :class="{ active: selectedCouponCode === c.couponCode, disabled: couponState(c).disabled }"
                @click="pickCoupon(c)">
                <div class="coupon-main">
                  <el-radio :model-value="selectedCouponCode" :value="c.couponCode"
                    :disabled="couponState(c).disabled">
                    <b>{{ c.templateName }}</b>
                  </el-radio>
                  <el-tag v-if="isRecommended(c)" type="success" size="small" effect="plain">推荐</el-tag>
                </div>
                <div class="coupon-meta">
                  <span class="coupon-face">¥{{ faceOf(c) }}</span>
                  <span class="coupon-sub">{{ [c.benefitText, c.benefit2Text].filter(Boolean).join(' · ') }}</span>
                  <span class="coupon-sub">{{ c.thresholdTips || '无门槛' }}</span>
                  <span class="coupon-sub">{{ validity(c) || '长期有效' }}</span>
                  <span class="mono coupon-sub">{{ c.couponCode }}</span>
                </div>
                <div class="coupon-state">
                  <el-tag v-if="couponState(c).disabled" type="danger" size="small" effect="plain">
                    {{ couponState(c).reason }}
                  </el-tag>
                  <span v-else-if="deductionOf(c) != null" class="coupon-ded">预计抵扣 ¥{{ deductionOf(c) }}</span>
                </div>
              </div>
            </div>
            <div class="pv-row pv-pay">
              <span class="pv-label">
                预计应付
                <el-tag v-if="est.scenario === 'zero'" type="success" size="small" effect="dark">0 元免支付</el-tag>
                <el-tag v-else-if="est.scenario === 'partial'" type="warning" size="small" effect="dark">差额支付</el-tag>
              </span>
              <span v-if="est.amount != null" class="price pay-big">{{ est.approximate ? '约 ' : '' }}¥{{ est.amount }}</span>
              <span v-else class="muted">以提交后实际结算为准</span>
            </div>
            <p v-if="est.approximate" class="muted est-note">已手动切换优惠券：金额为本地估算（仅识别「N 元」面额券），最终以提交订单时服务端结算为准。</p>
          </div>
        </div>

        <div class="step-actions">
          <el-button @click="backToGoods">返回重选商品</el-button>
          <el-button type="primary" :disabled="draftExpired" @click="step = 3">下一步 · 确认下单</el-button>
        </div>
      </template>

      <!-- 步骤4：确认下单 -->
      <template v-else-if="step === 3 && settleData">
        <div class="step-head">
          <h3 class="sec-title">订单确认</h3>
          <el-tag :type="draftExpired ? 'danger' : 'info'" effect="plain">
            草稿{{ draftExpired ? '已过期' : ` ${fmtCountdown(draftLeft)} 后过期` }}
          </el-tag>
        </div>
        <el-descriptions :column="1" border size="small" class="review-desc">
          <el-descriptions-item label="下单账号">{{ accountLabel }}</el-descriptions-item>
          <el-descriptions-item label="门店">{{ storeName }}（{{ storeNo }}）</el-descriptions-item>
          <el-descriptions-item label="商品">{{ settleData.preview.goods?.[0]?.name || goods.spuName }}</el-descriptions-item>
          <el-descriptions-item label="规格">{{ goods.skus?.find((s) => s.skuId === settleSkuIdUsed)?.specDesc || '默认' }}</el-descriptions-item>
          <el-descriptions-item label="数量">{{ settleQty }}</el-descriptions-item>
          <el-descriptions-item label="优惠券">{{ selectedCouponLabel || '不使用' }}</el-descriptions-item>
          <el-descriptions-item label="应付金额">
            <span v-if="est.amount != null" class="price pay-big">{{ est.approximate ? '约 ' : '' }}¥{{ est.amount }}</span>
            <span v-else class="muted">以实际结算为准</span>
          </el-descriptions-item>
        </el-descriptions>
        <div class="step-actions">
          <el-button @click="step = 2">上一步</el-button>
          <el-button type="primary" :disabled="draftExpired" :loading="createBusy" @click="dlgConfirm = true">
            提交订单
          </el-button>
        </div>
      </template>
    </div>

    <!-- 结果面板 -->
    <div v-if="result" class="page-card wb-body">

      <!-- 0 元单：直接成功 -->
      <template v-if="result.result === 'zero'">
        <el-result icon="success" title="下单成功 · 0 元免支付" sub-title="门店将真实制作饮品，请按时取餐；不取自然作废。">
          <template #extra>
            <div class="pickup-box">
              <div class="pickup-label">取餐码</div>
              <div class="pickup-big mono">{{ result.pickup_no || '—' }}</div>
            </div>
            <el-descriptions :column="1" border size="small" class="result-desc">
              <el-descriptions-item label="订单号"><span class="mono">{{ result.order_no }}</span></el-descriptions-item>
              <el-descriptions-item label="订单状态">
                <el-tag :type="orderStatusTag(result.status).type" size="small" effect="dark">
                  {{ result.status_label || orderStatusTag(result.status).label }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="支付金额"><span class="price">¥{{ result.pay_amount ?? '0' }}</span></el-descriptions-item>
              <el-descriptions-item v-if="result.coupon_code" label="使用券码">
                <span class="mono">{{ result.coupon_code }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="门店">{{ storeName }}</el-descriptions-item>
            </el-descriptions>
            <div class="step-actions center">
              <el-button type="primary" @click="router.push('/ops/pickup')">前往取餐查询</el-button>
              <el-button plain @click="restartFlow">再下一单</el-button>
            </div>
          </template>
        </el-result>
      </template>

      <!-- 差额支付：支付卡 -->
      <template v-else-if="result.result === 'partial'">
        <div class="step-head">
          <h3 class="sec-title">订单已创建 · 待支付（差额支付）</h3>
          <el-tag type="warning" effect="dark">待支付</el-tag>
        </div>

        <el-alert v-if="result.note" type="info" :closable="false" class="block-gap" :title="result.note" />

        <div class="pay-summary">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="订单号"><span class="mono">{{ result.order_no }}</span></el-descriptions-item>
            <el-descriptions-item label="支付单号"><span class="mono">{{ result.pay_no || '—' }}</span></el-descriptions-item>
            <el-descriptions-item label="应付金额">
              <span class="price pay-big">¥{{ result.pay_amount ?? result.total_amount }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="支付截止">{{ payDeadlineText }}</el-descriptions-item>
          </el-descriptions>
          <div class="pay-countdown-wrap">
            <div class="pickup-label">支付窗口剩余</div>
            <div class="pay-countdown mono" :class="{ expired: payExpired }">
              {{ payExpired ? '已过期' : fmtCountdown(payLeft) }}
            </div>
          </div>
        </div>

        <el-divider />

        <div class="mode-bar">
          <span class="pv-label">支付模式</span>
          <el-radio-group v-model="payMode">
            <el-radio value="manual">人工模式（默认）</el-radio>
            <el-radio value="auto">自动模式 <el-tag size="small" type="warning" effect="plain">实验性</el-tag></el-radio>
          </el-radio-group>
          <el-button v-if="paidStatus" size="small" type="primary" plain @click="router.push('/ops/pickup')">
            前往取餐查询
          </el-button>
        </div>

        <!-- 人工模式 -->
        <div v-if="payMode === 'manual'" class="mode-panel">
          <p class="guide">
            <el-icon><InfoFilled /></el-icon>
            复制支付串在支付宝完成支付（纯协议范围不含扣款动作）。
            <el-link v-if="result.h5_url" :href="result.h5_url" target="_blank" type="primary" style="vertical-align: baseline">
              打开 H5 收银台链接
            </el-link>
          </p>
          <el-collapse class="pay-str-collapse">
            <el-collapse-item name="str">
              <template #title>
                <span class="mono muted">支付串（order_str，点击展开 / 收起，{{ (result.order_str || '').length }} 字符）</span>
              </template>
              <el-input type="textarea" readonly :rows="6" :model-value="result.order_str" class="mono pay-str" />
            </el-collapse-item>
          </el-collapse>
          <div class="step-actions left">
            <el-button type="primary" plain :icon="CopyDocument" @click="copyPayStr">复制支付串</el-button>
            <el-button :icon="Refresh" :loading="continueBusy" :disabled="payExpired" @click="regenPayStr">
              重新生成支付串
            </el-button>
          </div>
        </div>

        <!-- 自动模式 -->
        <div v-else class="mode-panel">
          <p class="guide">
            <el-icon><WarningFilled /></el-icon>
            自动模式为实验性能力：由服务端调用支付宝 H5 收银台自动提交接缝，遇登录墙 / 风控交互会中止并返回指引。
          </p>

          <el-alert v-if="autoConfigNote" type="warning" :closable="false" class="block-gap"
            title="自动支付模块未配置（HTTP 501）" :description="autoConfigNote" show-icon />

          <div v-if="autoResult && autoResult.status === 'needs_interaction'" class="block-gap">
            <el-alert type="warning" :closable="false" show-icon title="自动支付需要人工交互"
              :description="autoResult.message || '请在支付宝侧完成登录 / 校验后重试。'" />
          </div>
          <div v-else-if="autoResult && autoResult.status === 'failed'" class="block-gap">
            <el-alert type="error" :closable="false" show-icon title="自动支付失败"
              :description="autoResult.message || '未知原因，请改用人工模式。'" />
          </div>

          <div v-if="autoPolling" class="poll-line">
            <el-icon class="is-loading"><Loading /></el-icon>
            已提交自动支付，每 5 秒轮询订单状态，直至离开「待支付」…
            <el-tag v-if="paidStatus" size="small" :type="orderStatusTag(paidStatus.status).type" effect="dark">
              {{ paidStatus.status_label || orderStatusTag(paidStatus.status).label }}
            </el-tag>
          </div>
          <el-alert v-else-if="paidStatus && paidStatus.status !== 1" type="success" :closable="false" class="block-gap"
            :title="`订单状态已更新：${paidStatus.status_label || orderStatusTag(paidStatus.status).label}（已离开待支付）`" />

          <div class="step-actions left">
            <el-button type="warning" :loading="autoBusy" :disabled="payExpired || !!paidStatus"
              @click="startAutoPay">
              发起自动支付（实验性）
            </el-button>
            <el-button v-if="autoPolling" plain @click="stopPolling(true)">停止轮询</el-button>
          </div>
        </div>

        <el-divider />
        <div class="step-actions">
          <el-button plain @click="restartFlow">再下一单</el-button>
        </div>
      </template>
    </div>

    <!-- SKU 抽屉 -->
    <el-drawer v-model="dlgGoods" :title="goods.spuName || '商品 SKU'" size="560px">
      <div v-if="goods.img" class="goods-hero">
        <el-image :src="goods.img" :preview-src-list="[goods.img, ...(goods.detailImages || [])]"
          preview-teleported fit="cover" class="goods-hero-img" />
      </div>
      <el-alert v-if="goods.description" :title="goods.description" type="info" :closable="false" class="goods-desc" />
      <!-- 属性选择（温度/甜度等）：默认项预选，无默认自动选首项；服务端要求每组属性必选 -->
      <div v-if="goods.attributes && goods.attributes.length" class="attr-groups">
        <div v-for="a in goods.attributes" :key="a.attributeId" class="attr-group">
          <div class="attr-title">{{ (a.name || '').trim() }}</div>
          <el-radio-group v-model="attrSel[a.attributeId]" size="small">
            <el-radio-button v-for="o in a.attrOptions" :key="o.attributeOptionId"
              :value="o.attributeOptionId" :disabled="o.saleOut">
              {{ o.name }}<span v-if="o.price && Number(o.price) > 0" class="attr-price">+¥{{ o.price }}</span>
            </el-radio-button>
          </el-radio-group>
        </div>
      </div>
      <!-- 加料选择：所有组渲染（气泡/茶冻等 0 元占位组必带，wire 实证 App 全带上送）；可选组可"不加" -->
      <div v-if="allExtras.length" class="attr-groups">
        <div v-for="e in allExtras" :key="e.extraId" class="attr-group">
          <div class="attr-title">{{ (e.name || '').trim() }}<el-tag v-if="isMustExtra(e)" size="small" type="warning" effect="plain" class="must-tag">必选</el-tag></div>
          <el-radio-group v-model="extraSel[e.extraId]" size="small">
            <el-radio-button v-if="!isMustExtra(e) && !hasZeroPricePreselect(e)" value="">不加</el-radio-button>
            <el-radio-button v-for="o in e.extraOptions" :key="o.skuId"
              :value="o.skuId" :disabled="(o.stock ?? 1) <= 0">
              {{ o.name }}<span v-if="Number(o.salePrice) > 0" class="attr-price">+¥{{ o.salePrice }}</span>
            </el-radio-button>
          </el-radio-group>
        </div>
      </div>
      <el-table :data="goods.skus" stripe size="small">
        <el-table-column label="规格" min-width="150">
          <template #default="{ row }">{{ row.specDesc || '默认' }}</template>
        </el-table-column>
        <el-table-column label="单价" width="85">
          <template #default="{ row }"><span class="price">¥{{ row.price }}</span></template>
        </el-table-column>
        <el-table-column label="库存" width="80">
          <template #default="{ row }">
            <el-tag :type="row.stock > 0 ? 'success' : 'danger'" size="small" effect="plain">
              {{ row.stock > 0 ? row.stock : '售罄' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="数量" width="130">
          <template #default="{ row }">
            <el-input-number v-model="skuQty[row.skuId]" :min="1" :max="99" size="small"
              :disabled="row.stock <= 0" controls-position="right" style="width: 110px" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" size="small" :disabled="row.stock <= 0"
              :loading="settleBusy && settleSkuId === row.skuId" @click="doSettle(row)">
              去结算
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <p class="muted drawer-note">选择数量后点击「去结算」生成 10 分钟有效试算草稿（服务端以该草稿完成下单）。</p>
    </el-drawer>

    <!-- 确认下单对话框（不可点击遮罩关闭） -->
    <el-dialog v-model="dlgConfirm" title="确认提交订单" width="560px"
      :close-on-click-modal="false" :close-on-press-escape="false">
      <el-descriptions v-if="settleData" :column="1" border size="small">
        <el-descriptions-item label="下单账号">{{ accountLabel }}</el-descriptions-item>
        <el-descriptions-item label="门店">{{ storeName }}（{{ storeNo }}）</el-descriptions-item>
        <el-descriptions-item label="商品">{{ settleData.preview.goods?.[0]?.name || goods.spuName }}</el-descriptions-item>
        <el-descriptions-item label="规格">{{ goods.skus?.find((s) => s.skuId === settleSkuIdUsed)?.specDesc || '默认' }}</el-descriptions-item>
        <el-descriptions-item label="数量">{{ settleQty }}</el-descriptions-item>
        <el-descriptions-item label="优惠券">{{ selectedCouponLabel || '不使用' }}</el-descriptions-item>
        <el-descriptions-item label="应付金额">
          <span v-if="est.amount != null" class="price pay-big">{{ est.approximate ? '约 ' : '' }}¥{{ est.amount }}</span>
          <span v-else class="muted">以实际结算为准</span>
        </el-descriptions-item>
      </el-descriptions>
      <el-alert v-if="isZeroPay" type="error" :closable="false" show-icon class="block-gap"
        title="0 元单会真实制作饮品！门店由你选择，不取自然作废" />
      <template #footer>
        <el-button @click="dlgConfirm = false">取消</el-button>
        <el-button type="primary" :loading="createBusy" :disabled="draftExpired" @click="doCreate">确认提交订单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, InfoFilled, Refresh, WarningFilled } from '@element-plus/icons-vue'
import { apiAccounts, apiOps } from '../api'
import { fmtCountdown, fmtTime, orderStatusTag } from '../utils/format'
import { nowMs } from '../utils/clock'

const router = useRouter()

/* ---------------- 全局流程状态 ---------------- */
const step = ref(0) // 0 选账号 / 1 选门店商品 / 2 试算选券 / 3 确认下单；结果面板时置 4
const narrow = ref(false)

/* 步骤1：账号 */
const accounts = ref([])
const accountId = ref(null)
const hasOnline = computed(() => accounts.value.some((a) => a.status === 'online'))
const accountLabel = computed(() => {
  const a = accounts.value.find((x) => x.id === accountId.value)
  return a ? `${a.label}（${a.phone_masked}）` : '—'
})

/* 步骤2：城市/门店/菜单（数据链同 MenuExplorerView，游客态） */
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
const storeName = computed(() => stores.value.find((s) => s.storeNo === storeNo.value)?.storeName || storeNo.value)

/* SKU 抽屉 */
const dlgGoods = ref(false)
const goods = ref({ skus: [] })
const skuQty = reactive({}) // skuId -> 数量（默认 1）
const attrSel = reactive({}) // attributeId -> 已选 attributeOptionId（打开抽屉时按默认/首项预选）
const extraSel = reactive({}) // extraId -> 已选加料 option 的 skuId（''=不加）
const allExtras = computed(() => goods.value.extras || [])
const isMustExtra = (e) => !!e?.extraPurchaseLimit?.must
const hasZeroPricePreselect = (e) =>
  (e?.extraOptions || []).some((o) => Number(o.salePrice) === 0 && (o.stock ?? 1) > 0)
const settleBusy = ref(false)
const settleSkuId = ref(null)

/* 步骤3：试算草稿 */
const settleData = ref(null)
const selectedCouponCode = ref('')
const draftLeft = ref(0)
const draftExpired = ref(false)
/* 本次草稿对应的 SKU / 数量快照（供步骤4 与确认对话框展示；settleSkuId 在请求结束后即复位） */
const settleSkuIdUsed = ref(null)
const settleQty = ref(1)
const selectedCouponLabel = computed(() => {
  const p = settleData.value?.preview
  const c = (p?.available_coupons || []).find((x) => x.couponCode === selectedCouponCode.value)
  return c ? couponLabel(c) : ''
})

/* 步骤4：确认与下单 */
const dlgConfirm = ref(false)
const createBusy = ref(false)

/* 结果面板 */
const result = ref(null)
const payLeft = ref(0)
const payExpired = ref(false)
/* 支付截止绝对锚点（epoch ms，服务端钳制后 pay_deadline_ts；倒计时由它与校准时钟推算，不随续付重置） */
const payDeadlineTs = ref(0)
const payMode = ref('manual')
const continueBusy = ref(false)
const autoBusy = ref(false)
const autoResult = ref(null) // { status, message }
const autoConfigNote = ref('')
const autoPolling = ref(false)
const paidStatus = ref(null) // 轮询到的 { status, status_label }

/* 定时器（onUnmounted 统一清理） */
let draftTimer = null
let payTimer = null
let pollTimer = null
let pollBusy = false

/* ---------------- 派生：预计应付（本地视角） ----------------
 * 服务端 estimated_pay 基于「推荐券」；切换券后无法重算（无 settle-with-coupon 端点），
 * 仅对「N 元」面额券做本地估算并标注「约」，其余提示以实际结算为准。 */
const est = computed(() => {
  const p = settleData.value?.preview
  if (!p) return { amount: null, approximate: false, scenario: '' }
  const total = Number(p.total_trade_price || 0)
  if (!selectedCouponCode.value) {
    return { amount: p.total_trade_price, approximate: false, scenario: total > 0 ? 'partial' : 'zero' }
  }
  const rec = p.recommended_coupon?.couponCode
  if (rec && selectedCouponCode.value === rec) {
    return { amount: p.estimated_pay, approximate: false, scenario: p.scenario_preview }
  }
  const c = (p.available_coupons || []).find((x) => x.couponCode === selectedCouponCode.value)
  const m = (c?.benefitText || '').match(/([\d.]+)\s*元/)
  if (m) {
    const pay = Math.max(total - Number(m[1]), 0)
    return { amount: pay.toFixed(2), approximate: true, scenario: pay > 0 ? 'partial' : 'zero' }
  }
  return { amount: null, approximate: true, scenario: '' }
})
const isZeroPay = computed(() => est.value.amount != null && Number(est.value.amount) === 0)
/* 支付截止展示：优先用服务端钳制锚点 payDeadlineTs（本地 HH:mm:ss），回退旧字段 expire_at 文本 */
const payDeadlineText = computed(() => {
  if (Number(payDeadlineTs.value) > 0) {
    const d = new Date(payDeadlineTs.value)
    const p = (n) => String(n).padStart(2, '0')
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  }
  return fmtTime(result.value?.expire_at)
})

/* ---------------- 初始化 ---------------- */
onMounted(async () => {
  onResize()
  window.addEventListener('resize', onResize)
  const data = await apiAccounts.list({ page: 1, page_size: 100 })
  accounts.value = data.items
  const online = data.items.find((a) => a.status === 'online')
  if (online) accountId.value = online.id
  loadingCities.value = true
  try {
    const c = await apiOps.cities()
    cities.value = c.cities
  } finally {
    loadingCities.value = false
  }
})

/* 账号切换：清理下游全部状态（草稿/结果/计时器），回到步骤1 */
watch(accountId, () => {
  clearResult()
  clearSettle()
  if (step.value > 0) step.value = 0
})

/* ---------------- 步骤1 → 2 数据链 ---------------- */
async function onCityChange() {
  storeNo.value = ''
  menuMeta.value = null
  clearSettle()
  clearResult()
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

function onStoreChange() {
  clearSettle()
  clearResult()
  if (storeNo.value) loadMenu()
}

async function loadMenu() {
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

const pagedMenu = computed(() => {
  const items = menuMeta.value?.items || []
  return items.slice((page.value - 1) * pageSize, page.value * pageSize)
})

function rowClassName({ row }) {
  return row.saleOut ? 'row-saleout' : ''
}

async function openGoods(row) {
  if (row.saleOut) return // 售罄行禁用点击
  dlgGoods.value = true
  goods.value = { spuId: row.spuId, spuName: row.spuName, img: row.img || '', description: '', skus: [], detailImages: [] }
  Object.keys(attrSel).forEach((k) => delete attrSel[k])
  Object.keys(extraSel).forEach((k) => delete extraSel[k])
  try {
    const detail = await apiOps.goods(row.spuId, storeNo.value)
    goods.value = detail
    detail.skus.forEach((s) => {
      if (!(s.skuId in skuQty)) skuQty[s.skuId] = 1
    })
    // 属性预选：defaulted 项优先，无默认取首个可选项（服务端要求每组属性必选）
    for (const a of detail.attributes || []) {
      const opts = (a.attrOptions || []).filter((o) => !o.saleOut)
      const chosen = opts.find((o) => o.defaulted) || opts[0]
      if (chosen) attrSel[a.attributeId] = chosen.attributeOptionId
    }
    // 加料组预选：defaulted → 首个 0 元占位项（无气泡/无茶冻类，wire 实证 App 必带上送）
    // → must 组强制首项；纯付费加料组不预选（"不加"）
    for (const e of detail.extras || []) {
      const opts = (e.extraOptions || []).filter((o) => (o.stock ?? 1) > 0)
      const chosen = opts.find((o) => o.defaulted)
        || opts.find((o) => Number(o.salePrice) === 0)
        || (e?.extraPurchaseLimit?.must ? opts[0] : undefined)
      extraSel[e.extraId] = chosen ? chosen.skuId : ''
    }
  } catch {
    dlgGoods.value = false
  }
}

function backToAccount() {
  step.value = 0
}

/* ---------------- 步骤2 → 3：试算（settle） ---------------- */
async function doSettle(sku) {
  if (!accountId.value) {
    ElMessage.warning('请先选择账号')
    return
  }
  const qty = Number(skuQty[sku.skuId] || 1)
  settleSkuId.value = sku.skuId
  settleBusy.value = true
  try {
    const payload = {
      store_no: storeNo.value,
      store_name: storeName.value,
      spu_id: String(goods.value.spuId || ''),
      spu_name: goods.value.spuName || '',
      sku_id: String(sku.skuId || ''),
      sku_name: sku.name || sku.specDesc || '默认规格',
      item_sku_id: sku.itemSkuId != null ? String(sku.itemSkuId) : undefined,
      quantity: qty,
      image_url: goods.value.img || '',
      spu_type: goods.value.spuType || 'stand',
    }
    // spec_list 由 SKU 的 specOptionInfos 映射（含名称字段，wire 实证全量透传）；无则省略
    const specs = (sku.specOptionInfos || [])
      .filter((o) => o && o.specId && o.specOptionId)
      .map((o) => ({
        specId: String(o.specId), specOptionId: String(o.specOptionId),
        specName: o.specName || '', specOptionName: o.specOptionName || '',
      }))
    if (specs.length) payload.spec_list = specs
    // attribute_list：按抽屉属性选择器构造（预选 defaulted/首项，可手动改）——服务端要求每组必选
    const attrs = []
    for (const a of goods.value.attributes || []) {
      const selId = attrSel[a.attributeId]
      const opt = (a.attrOptions || []).find((o) => String(o.attributeOptionId) === String(selId))
      if (opt) {
        attrs.push({
          attributeId: String(a.attributeId), attributeName: a.name || '',
          attributeOptionId: String(opt.attributeOptionId), attributeOptionName: opt.name || '',
        })
      }
    }
    payload.attribute_list = attrs
    // extra_list：所有加料组的当前选择（0 元占位组必在；可选组未选则不提交该组）
    const extras = []
    for (const e of goods.value.extras || []) {
      const opt = (e.extraOptions || []).find((o) => String(o.skuId) === String(extraSel[e.extraId]))
      if (opt) extras.push(opt)
    }
    payload.extra_list = extras
    // calculatePrice 入参与 settle 行所需
    payload.sale_price = Number(sku.price) || 0
    payload.nutrition_info = pickNutrition(sku, attrs)

    const data = await apiOps.orderSettle(accountId.value, payload)
    settleData.value = data
    selectedCouponCode.value = data.preview?.recommended_coupon?.couponCode || ''
    settleSkuIdUsed.value = sku.skuId
    settleQty.value = qty
    startDraftCountdown(data.expires_at)
    dlgGoods.value = false
    step.value = 2
    ElMessage.success('试算完成，草稿 10 分钟内有效')
  } finally {
    settleBusy.value = false
    settleSkuId.value = null
  }
}

function startDraftCountdown(expiresAt) {
  stopDraftTimer()
  draftExpired.value = false
  const end = new Date(String(expiresAt).replace(' ', 'T')).getTime()
  if (!Number.isFinite(end)) return
  const tick = () => {
    draftLeft.value = Math.floor((end - nowMs()) / 1000)
    if (draftLeft.value <= 0) {
      draftLeft.value = 0
      draftExpired.value = true
      stopDraftTimer()
      ElMessage.warning('试算草稿已过期，请返回重新试算')
    }
  }
  tick()
  draftTimer = setInterval(tick, 1000)
}

function backToGoods() {
  clearSettle()
  step.value = 1
}

/* ---------------- 步骤4：确认下单 ---------------- */
async function doCreate() {
  if (!settleData.value) return
  createBusy.value = true
  try {
    const res = await apiOps.orderCreate(accountId.value, {
      draft_id: settleData.value.draft_id,
      coupon_code: selectedCouponCode.value || null,
    })
    result.value = res
    stopDraftTimer()
    dlgConfirm.value = false
    step.value = 4
    if (res.result === 'zero') {
      ElMessage.success(`下单成功，取餐码 ${res.pickup_no || '—'}`)
    } else {
      // 优先取服务端钳制后的支付截止绝对锚点（epoch ms，= 下单+10min，响应携带 server_time 已校准时钟）；
      // 无该字段时回退按窗口秒数从校准时钟推算
      const deadline = Number(res.pay_deadline_ts)
      payDeadlineTs.value = deadline > 0 ? deadline : nowMs() + Number(res.pay_window_seconds || 600) * 1000
      payExpired.value = false
      startPayTimer()
      ElMessage.success('订单已创建，请在支付窗口内完成支付')
    }
  } finally {
    createBusy.value = false
  }
}

/* 支付倒计时：绝对时间法 —— 每秒用「截止锚点 - 校准时钟」重算剩余，杜绝累计漂移与续付重置；
 * 与后端钳制语义（pay_deadline 锚定下单时刻）一致，客户端时钟偏移由 utils/clock 全局校正 */
function startPayTimer() {
  stopPayTimer()
  if (!(Number(payDeadlineTs.value) > 0)) return // 无截止锚点不启动（正常差额单不会出现）
  const tick = () => {
    payLeft.value = Math.max(0, Math.round((payDeadlineTs.value - nowMs()) / 1000))
    if (payLeft.value <= 0) {
      payLeft.value = 0
      payExpired.value = true
      stopPayTimer()
      stopPolling()
      ElMessage.warning('支付窗口已过期，订单已自动取消；可到取餐查询页核实订单状态')
    }
  }
  tick()
  payTimer = setInterval(tick, 1000)
}

/* ---------------- 结果面板：支付操作 ---------------- */
async function regenPayStr() {
  continueBusy.value = true
  try {
    const res = await apiOps.orderContinuePay(accountId.value, result.value.order_no)
    result.value = { ...result.value, ...res }
    // 续付不重置支付窗口：截止锚定下单时刻（后端钳制，continue-pay 返回的 pay_deadline_ts 不变或更小）。
    // 仅当响应携带有效 pay_deadline_ts 时更新锚点，无该字段则维持原值 —— 倒计时自然不漂移
    const deadline = Number(res.pay_deadline_ts)
    if (deadline > 0) payDeadlineTs.value = deadline
    payExpired.value = false // 续付会重新 issued，复位过期标记
    startPayTimer()
    ElMessage.success('已重新生成支付串（支付截止不重置，仍以下单时刻为准）')
  } finally {
    continueBusy.value = false
  }
}

async function copyPayStr() {
  const text = result.value?.order_str || ''
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('支付串已复制到剪贴板')
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      ElMessage.success('支付串已复制')
    } catch {
      ElMessage.error('复制失败，请在展开的支付串中手动全选复制')
    }
    document.body.removeChild(ta)
  }
}

/* 自动模式：两道确认 → orderPay(auto) → 视返回启动轮询 */
async function startAutoPay() {
  try {
    await ElMessageBox.confirm(
      '自动支付为实验性功能：将由服务端调用支付宝 H5 收银台自动提交接缝，可能因登录态 / 风控交互而中止。确定继续？',
      '自动支付 · 第 1 次确认',
      { type: 'warning', confirmButtonText: '继续', cancelButtonText: '取消' },
    )
    await ElMessageBox.confirm(
      '再次确认：本次操作将触发支付提交流程（跳过人工确认环节），失败请改用人工模式。确定发起自动支付？',
      '自动支付 · 第 2 次确认',
      { type: 'warning', confirmButtonText: '确认发起', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  autoBusy.value = true
  autoResult.value = null
  autoConfigNote.value = ''
  try {
    const res = await apiOps.orderPay(accountId.value, result.value.order_no, 'auto')
    autoResult.value = res
    if (res.status === 'submitted') {
      startPolling()
      ElMessage.success('自动支付已提交，开始轮询订单状态')
    } else if (res.status === 'needs_interaction') {
      ElMessage.warning(res.message || '自动支付需要人工交互')
    } else {
      ElMessage.error(res.message || '自动支付失败')
    }
  } catch (err) {
    // 501：自动支付模块未配置，持久化展示后端 detail 的配置说明（ElMessage 已由全局拦截器弹出）
    if (err?.response?.status === 501) {
      const d = err.response.data?.detail
      autoConfigNote.value = typeof d === 'string' ? d : d ? JSON.stringify(d) : '自动支付模块未配置或未安装。'
    }
  } finally {
    autoBusy.value = false
  }
}

function startPolling() {
  stopPolling()
  autoPolling.value = true
  pollTimer = setInterval(async () => {
    if (pollBusy || !result.value) return
    pollBusy = true
    try {
      const s = await apiOps.orderStatus(accountId.value, result.value.order_no)
      paidStatus.value = s
      if (s.status !== 1) {
        stopPolling()
        ElMessage.success(`订单状态已更新：${s.status_label || orderStatusTag(s.status).label}`)
      }
    } catch {
      /* 单次探针失败忽略（全局拦截器已提示），下一轮继续 */
    } finally {
      pollBusy = false
    }
  }, 5000)
}

function stopPolling(manual) {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
  autoPolling.value = false
  if (manual) ElMessage.info('已停止状态轮询')
}

/* ---------------- 复位与清理 ---------------- */
function clearSettle() {
  settleData.value = null
  selectedCouponCode.value = ''
  draftExpired.value = false
  draftLeft.value = 0
  stopDraftTimer()
}

function clearResult() {
  result.value = null
  payMode.value = 'manual'
  payExpired.value = false
  payLeft.value = 0
  payDeadlineTs.value = 0
  autoResult.value = null
  autoConfigNote.value = ''
  paidStatus.value = null
  stopPayTimer()
  stopPolling()
}

function restartFlow() {
  clearResult()
  clearSettle()
  step.value = 1 // 保留账号与门店选择，快速再下一单
}

function stopDraftTimer() {
  if (draftTimer) clearInterval(draftTimer)
  draftTimer = null
}
function stopPayTimer() {
  if (payTimer) clearInterval(payTimer)
  payTimer = null
}

function onResize() {
  narrow.value = window.innerWidth < 860
}

onUnmounted(() => {
  stopDraftTimer()
  stopPayTimer()
  stopPolling()
  window.removeEventListener('resize', onResize)
})

/* ---------------- 展示辅助 ---------------- */
/* 按已选属性组合匹配营养信息（nutritionInfos 按 attrOptionIds 键控；无组合键条目兜底） */
function pickNutrition(sku, attrs) {
  const list = sku.nutrition_infos || sku.nutritionInfos || []
  if (!list.length) return null
  const selIds = new Set(attrs.map((a) => String(a.attributeOptionId)))
  return (list.find((n) => (n.attrOptionIds || []).every((id) => selIds.has(String(id))))
       || list.find((n) => !n.attrOptionIds || !n.attrOptionIds.length)
       || null)
}

function couponLabel(c) {
  return [c.templateName, c.benefitText, c.thresholdTips].filter(Boolean).join(' · ')
}
function validity(c) {
  if (!c.useStartTime && !c.useEndTime) return ''
  return `${c.useStartTime ? fmtTime(c.useStartTime) : '?'} ~ ${c.useEndTime ? fmtTime(c.useEndTime) : '?'}`
}

/* ---------------- 券选择卡片：可用性校验（与服务端五重验证同步的前置展示） ---------------- */
const couponOptions = computed(() => settleData.value?.preview?.available_coupons || [])
const disabledCount = computed(() => couponOptions.value.filter((c) => couponState(c).disabled).length)

function faceOf(c) {
  const m = (c.benefitText || '').match(/([\d.]+)\s*元/)
  return m ? m[1] : '—'
}
function deductionOf(c) {
  const m = (c.benefitText || '').match(/([\d.]+)\s*元/)
  if (!m) return null
  const total = Number(settleData.value?.preview?.total_trade_price || 0)
  return Math.min(Number(m[1]), total).toFixed(2)
}
function couponState(c) {
  const now = Date.now()
  if (c.canDiscount === false) return { disabled: true, reason: c.unavailableReason || '当前不可用' }
  if (typeof c.useEndTime === 'number' && now > c.useEndTime) return { disabled: true, reason: '已过期' }
  if (typeof c.useStartTime === 'number' && now < c.useStartTime) return { disabled: true, reason: '未生效' }
  const m = (c.thresholdTips || '').match(/满\s*([\d.]+)\s*元/)
  if (m) {
    const total = Number(settleData.value?.preview?.total_trade_price || 0)
    if (total < Number(m[1])) return { disabled: true, reason: `满${m[1]}元可用` }
  }
  return { disabled: false, reason: '' }
}
function isRecommended(c) {
  return settleData.value?.preview?.recommended_coupon?.couponCode === c.couponCode
}
function pickCoupon(c) {
  if (c && couponState(c).disabled) return
  selectedCouponCode.value = c ? c.couponCode : ''
}
</script>

<style scoped>
.wb-steps {
  margin-top: 6px;
}
.wb-body {
  margin-top: 16px;
}
.step-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.head-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.sec-title {
  margin: 0;
  font-size: 16px;
  color: var(--tea-800);
}
.acc-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.acc-tip {
  margin-top: 14px;
}
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
.muted {
  color: var(--muted);
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
.wb-menu :deep(.row-saleout) {
  cursor: not-allowed;
  opacity: 0.55;
}
.goods-hero {
  margin-bottom: 14px;
}
.goods-hero-img {
  width: 100%;
  height: 200px;
  border-radius: 12px;
}
.goods-desc {
  margin-bottom: 14px;
}
.attr-groups {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 14px;
  padding: 12px;
  border: 1px solid #e3ebe7;
  border-radius: 10px;
  background: #fafcfa;
}
.attr-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--tea-800);
  margin-bottom: 6px;
}
.attr-price {
  margin-left: 4px;
  color: #c45656;
  font-size: 12px;
}
.drawer-note {
  margin-top: 12px;
  font-size: 12.5px;
}
/* 试算预览 */
.preview-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.preview-col {
  background: var(--cream);
  border-radius: 12px;
  padding: 14px 16px;
}
.pv-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px dashed #e3eae6;
  font-size: 14px;
}
.pv-row:last-child {
  border-bottom: none;
}
.pv-total {
  font-weight: 600;
  color: var(--tea-800);
}
.pv-pay {
  padding-top: 12px;
}
.pv-label {
  color: var(--muted);
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.pay-big {
  font-size: 22px;
}
.est-note {
  font-size: 12px;
  margin: 6px 0 0;
}
.review-desc {
  max-width: 560px;
}
.pay-str-collapse {
  margin-bottom: 6px;
}
.coupon-head {
  padding: 7px 0 8px;
}
.coupon-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 320px;
  overflow-y: auto;
  margin-bottom: 10px;
}
.coupon-item {
  border: 1.5px solid #e3ebe7;
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.coupon-item:hover {
  border-color: var(--tea-500);
}
.coupon-item.active {
  border-color: var(--tea-600);
  background: var(--tea-100);
}
.coupon-item.disabled {
  opacity: 0.62;
  cursor: not-allowed;
  background: #fafbfa;
}
.coupon-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.coupon-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 4px 0 0 22px;
}
.coupon-face {
  color: #c45656;
  font-weight: 700;
  font-size: 15px;
}
.coupon-sub {
  font-size: 12.5px;
  color: var(--muted);
}
.coupon-ded {
  font-size: 12.5px;
  color: var(--tea-700);
  font-weight: 600;
}
.step-actions {
  display: flex;
  gap: 12px;
  margin-top: 20px;
  justify-content: flex-end;
  flex-wrap: wrap;
}
.step-actions.left {
  justify-content: flex-start;
}
.step-actions.center {
  justify-content: center;
}
.block-gap {
  margin-bottom: 14px;
}
/* 结果面板 */
.result-desc {
  max-width: 460px;
  margin: 0 auto;
}
.pickup-box {
  text-align: center;
  margin-bottom: 16px;
}
.pickup-label {
  color: var(--muted);
  font-size: 13px;
  margin-bottom: 4px;
}
.pickup-big {
  font-size: 42px;
  font-weight: 700;
  letter-spacing: 6px;
  color: var(--tea-700);
}
.pay-summary {
  display: flex;
  gap: 18px;
  align-items: stretch;
  flex-wrap: wrap;
}
.pay-summary .el-descriptions {
  flex: 1;
  min-width: 320px;
}
.pay-countdown-wrap {
  min-width: 180px;
  background: var(--cream);
  border-radius: 12px;
  padding: 12px 18px;
  text-align: center;
}
.pay-countdown {
  font-size: 30px;
  font-weight: 700;
  color: #c45656;
}
.pay-countdown.expired {
  color: var(--muted);
  font-size: 22px;
}
.mode-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.mode-panel {
  background: var(--cream);
  border-radius: 12px;
  padding: 14px 16px;
}
.guide {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin: 0 0 12px;
  font-size: 13.5px;
  color: var(--tea-800);
}
.pay-str :deep(textarea) {
  word-break: break-all;
  font-size: 11px;
}
.poll-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 13.5px;
  color: var(--tea-800);
  padding: 4px 0;
}
@media (max-width: 900px) {
  .preview-grid {
    grid-template-columns: 1fr;
  }
}
</style>
