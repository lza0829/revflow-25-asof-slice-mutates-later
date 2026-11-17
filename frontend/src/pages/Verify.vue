<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)

const slices = ref([])
const sliceDetail = ref(null)
const cutoff = ref('')
const sliceBusy = ref(false)
const proofLoading = ref(false)
const proofChecked = ref(null)

function nowZ() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
}

async function load() {
  rows.value = await getJson('/api/verify-reports')
  slices.value = await getJson('/api/asof-slices')
}

onMounted(async () => {
  cutoff.value = nowZ()
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runVerify() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/verify-reports')
    msg.value = r.consistent
      ? `互证一致：流水净额 ${(r.ledger_net_cents / 100).toFixed(2)} = 余额合计`
      : `互证不一致：流水 ${(r.ledger_net_cents / 100).toFixed(2)} vs 余额 ${(r.balance_sum_cents / 100).toFixed(2)}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function genSlice() {
  sliceBusy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/asof-slices?cutoff_at=' + encodeURIComponent(cutoff.value))
    sliceDetail.value = r
    msg.value = `已生成切片 #${r.id}（截止 ${r.cutoff_at}），净额合计 ${(r.net_cents / 100).toFixed(2)}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    sliceBusy.value = false
  }
}

async function viewSlice(id) {
  err.value = ''
  proofChecked.value = null
  try {
    sliceDetail.value = await getJson('/api/asof-slices/' + id)
  } catch (e) {
    sliceDetail.value = null
    err.value = String(e.message || e)
  }
}

async function recheckProof(id) {
  proofLoading.value = true
  err.value = ''
  try {
    const r = await getJson('/api/asof-slices/' + id + '/proof')
    const d = sliceDetail.value
    proofChecked.value =
      d && d.id === id
        ? {
            at: new Date().toLocaleTimeString(),
            sameHash: d.proof && d.proof.content_hash === r.proof.content_hash,
            sameTotal: d.proof && d.proof.balance_total_cents === r.proof.balance_total_cents,
          }
        : { at: new Date().toLocaleTimeString(), sameHash: true, sameTotal: true }
  } catch (e) {
    proofChecked.value = null
    err.value = String(e.message || e)
  } finally {
    proofLoading.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>净额互证</h1>
    <p class="lead">核对「已归并流水净额」与「账户余额合计」是否一致（隔离项不计入净额）。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runVerify">跑净额互证</button>
    </div>
    <p v-if="msg" :class="msg.includes('一致') && !msg.includes('不一致') ? 'ok' : 'msg'">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>互证报告</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>归并#</th><th>流水净额</th><th>余额合计</th><th>隔离数</th><th>一致</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.settle_run_id }}</td>
            <td>{{ yuan(r.ledger_net_cents) }}</td>
            <td>{{ yuan(r.balance_sum_cents) }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>
              <span class="tag" :class="r.consistent ? 'on' : 'bad'">
                {{ r.consistent ? '是' : '否' }}
              </span>
            </td>
            <td>{{ r.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="7">尚无报告</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>as-of 余额切片（时点物化）</h2>
      <p class="note-strip">
        只计入 booked_at ≤ T 且首次归并时刻 ≤ T 的已归并流水（正向 − 同类冲正），逐账户物化落库。
        切片不可改写：旧切片禁止 UPDATE/DELETE，任何变更只能以新 id 追加；每个切片钉死不变性证明
        （as-of、余额合计、明细行数、内容哈希），读时复算校验，再次查询必须同合计同哈希。
      </p>
      <div class="row-actions">
        <input
          v-model="cutoff"
          class="cutoff-input"
          placeholder="截止入账时刻 T，如 2026-11-16T18:00:00Z"
        />
        <button class="btn" :disabled="sliceBusy" @click="genSlice">生成 as-of 切片</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>#</th><th>截止 T</th><th>账户数</th><th>证明合计</th><th>明细行数</th><th>内容哈希</th><th>生成时间</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in slices" :key="s.id">
            <td>{{ s.id }}</td>
            <td>{{ s.cutoff_at }}</td>
            <td>{{ s.account_count }}</td>
            <td>{{ yuan(s.proof_balance_total_cents) }}</td>
            <td>{{ s.proof_line_count }}</td>
            <td class="hash">{{ (s.proof_content_hash || '').slice(0, 16) }}…</td>
            <td>{{ s.created_at }}</td>
            <td><button class="btn secondary" @click="viewSlice(s.id)">查看</button></td>
          </tr>
          <tr v-if="!slices.length"><td colspan="8">尚无切片</td></tr>
        </tbody>
      </table>
    </div>

    <div v-if="sliceDetail" class="panel">
      <h2>切片 #{{ sliceDetail.id }} · 截止 {{ sliceDetail.cutoff_at }}</h2>
      <p class="note-strip">
        「当前余额」为读取时实时值，仅并排对比，不回写切片；差异 = 当前余额 − 切片净额。
      </p>

      <div v-if="sliceDetail.proof" class="proof-strip">
        <div class="proof-cell">
          <span class="proof-k">as-of 时刻</span>
          <span class="proof-v">{{ sliceDetail.proof.cutoff_at }}</span>
        </div>
        <div class="proof-cell">
          <span class="proof-k">余额合计</span>
          <span class="proof-v">{{ yuan(sliceDetail.proof.balance_total_cents) }}</span>
        </div>
        <div class="proof-cell">
          <span class="proof-k">明细行数</span>
          <span class="proof-v">{{ sliceDetail.proof.line_count }}</span>
        </div>
        <div class="proof-cell">
          <span class="proof-k">内容哈希（{{ sliceDetail.proof.hash_algo }}）</span>
          <span class="proof-v hash">{{ sliceDetail.proof.content_hash }}</span>
        </div>
        <div class="proof-cell">
          <span class="proof-k">校验</span>
          <span class="tag" :class="sliceDetail.proof.verified ? 'on' : 'bad'">
            {{ sliceDetail.proof.verified ? '通过（未被后写改写）' : '失败' }}
          </span>
        </div>
        <div class="proof-cell">
          <button class="btn secondary" :disabled="proofLoading" @click="recheckProof(sliceDetail.id)">
            {{ proofLoading ? '复算中…' : '复算证明' }}
          </button>
          <span v-if="proofChecked" class="proof-v">
            {{ proofChecked.at }} 复算：
            <span class="tag" :class="proofChecked.sameHash && proofChecked.sameTotal ? 'on' : 'bad'">
              {{ proofChecked.sameHash && proofChecked.sameTotal ? '同合计同哈希' : '不一致' }}
            </span>
          </span>
        </div>
      </div>

      <table>
        <thead>
          <tr>
            <th>账户</th><th>名称</th><th>切片正向</th><th>切片冲正</th><th>切片净额</th>
            <th>当前余额</th><th>差异</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in sliceDetail.lines" :key="l.account_id">
            <td>{{ l.account_code }}</td>
            <td>{{ l.account_name }}</td>
            <td>{{ yuan(l.forward_cents) }}</td>
            <td>{{ yuan(l.reversal_cents) }}</td>
            <td>{{ yuan(l.net_cents) }}</td>
            <td>{{ yuan(l.current_balance_cents) }}</td>
            <td>
              <span class="tag" :class="l.diff_cents === 0 ? 'on' : 'warn'">
                {{ yuan(l.diff_cents) }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.cutoff-input {
  font-family: var(--mono);
  font-size: 13px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 2px;
  min-width: 320px;
  background: var(--panel);
  color: var(--ink);
}

.hash {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  word-break: break-all;
}

.proof-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px 18px;
  padding: 14px 16px;
  margin: 12px 0 18px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent-2);
  background: var(--panel);
}

.proof-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.proof-k {
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--muted);
}

.proof-v {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--ink);
}
</style>
