'use strict';
/* =====================================================================
web/static/app.js —— SCADA 监控大屏前端逻辑（班次2）
======================================================================
职责：
    1. 多 CDN 依次回退加载 ECharts 5 与 echarts-gl（bar3D 垛型用）；
    2. REST 轮询：/api/status(1s) /api/kpi(2s) /api/pallet3d(1s)
       /api/warehouse/locations(3s)；
    3. WebSocket 实时事件推送（ws://host:5081/ws），断线重连，
       连续失败自动降级为 REST /api/events 轮询；
    4. 六大图表/面板渲染：流程图(DOM)、趋势折线、NG率仪表盘、垛型3D、
       库位热力图、AGV 物流地图；事件滚动表与设备一览表；
    5. 控制按钮：POST /api/command → Plant.execute_command()。
约定：所有产量/NG率等指标均为仿真验证值。
====================================================================== */

/* ---------------- 常量与全局 ---------------- */
// 修复记录：echarts 核心与 echarts-gl 必须版本配对——gl 2.0.9 停更于 echarts 5.1~5.3 时代，
// 配 5.5 时实测"grid3D 坐标盒正常但 bar3D 柱体静默不渲染"。降至社区稳配 5.2.2。
// 修复记录：Edge 跟踪防护会拦截 CDN echarts（F12 实证 → ReferenceError:
// echarts is not defined），故首选同源本地 vendor 文件，CDN 全链降为备用；
// 版本配对：gl 2.0.9 对应 echarts 5.1~5.3 时代，社区稳配 5.2.2。
const ECHARTS_CDNS = [
  '/static/vendor/echarts-5.2.2.min.js',
  'https://cdn.jsdelivr.net/npm/echarts@5.2.2/dist/echarts.min.js',
  'https://cdn.bootcdn.net/ajax/libs/echarts/5.2.2/echarts.min.js',
  'https://registry.npmmirror.com/echarts/5.2.2/files/dist/echarts.min.js',
  'https://unpkg.com/echarts@5.2.2/dist/echarts.min.js',
];
// 修复记录：bootcdn 的 echarts-gl@2.0.9 路径实测 404，替换为 npmmirror 源
const GL_CDNS = [
  '/static/vendor/echarts-gl-2.0.9.min.js',
  'https://cdn.jsdelivr.net/npm/echarts-gl@2.0.9/dist/echarts-gl.min.js',
  'https://registry.npmmirror.com/echarts-gl/2.0.9/files/dist/echarts-gl.min.js',
  'https://unpkg.com/echarts-gl@2.0.9/dist/echarts-gl.min.js',
];
const WS_PORT = 5081;                 // 与 config/settings.py SCADA_WS_PORT 一致
const STATE_COLOR = {
  '运行': '#00e676', '待机': '#ffd54f', '停止': '#90a4ae',
  '故障': '#ff5252', '维护': '#40c4ff',
};
const EVT_LABEL = {
  'device.state': '设备状态', 'fault.raised': '故障产生', 'fault.cleared': '故障清除',
  'flow.product_out': '产品流出', 'vision.ok': '质检OK', 'vision.ng': '质检NG',
  'pallet.box_placed': '码垛放箱', 'pallet.full': '满托完成', 'agv.call': 'AGV呼叫',
  'agv.task_created': '任务建档', 'agv.phase': 'AGV阶段', 'agv.task_done': '任务完成',
  'wh.inbound_done': '入库完成', 'wh.outbound_done': '出库完成',
  'clock.pause': '时钟暂停', 'clock.resume': '时钟恢复',
  'assembly.door_hold': '门开保持', 'assembly.door_resume': '关门恢复',
  'ui.command': '大屏命令',
  // 班次3修改：MES/EMS 事件中文名
  'mes.order_created': '工单开立', 'mes.order_closed': '工单关单',
  'ems.health_alert': '健康告警', 'ems.maintenance': '维护动作',
};

const charts = {};                    // echarts 实例集合
let glReady = false;                  // echarts-gl 是否加载成功
let ws = null;                        // WebSocket 会话
let wsRetry = 0;                      // 重连计数（超过3次降级为轮询）
let wsFallbackTimer = null;           // REST 降级轮询句柄
let lastBoxesKey = '';                // 垛型去重键（减少无谓 setOption）
let lastLocationsJson = '';           // 库位表去重
let evSeqMax = -1;                    // 已收最大事件 seq（REST 拉取增量判断）

const $ = (id) => document.getElementById(id);

/* ---------------- 启动入口 ---------------- */
window.addEventListener('DOMContentLoaded', () => {
  bindButtons();
  connectWS();
  setInterval(pollStatus, 1000);
  setInterval(pollKpi, 2000);
  setInterval(pollPallet, 1000);
  setInterval(pollLocations, 3000);
  // 班次3修改：MES 工单面板(3s) 与 EMS 能耗/健康面板(2s) 轮询
  setInterval(pollMes, 3000);
  setInterval(pollEms, 2000);
  pollStatus(); pollPallet(); pollLocations(); pollMes(); pollEms();
  loadScripts(ECHARTS_CDNS).then((ok) => {
    if (!ok) { toast('ECharts CDN 全部加载失败，图表降级为表格模式', true); return; }
    initCharts();
    refreshTrendFromCache();          // 立即画一版已缓存数据
    drawAgvFromCache();
    loadScripts(GL_CDNS).then((okGl) => {
      // 修复记录（规格4⑦语义修正）：脚本加载成功 ≠ 能用——bar3D 依赖 WebGL。
      // WebGL/加载库任一缺失时不禁用默认等距视图，仅禁用"真3D"选项：
      // 视图循环自动跳过、绘制分发防御性回退等距，提示文案与实际行为一致。
      glReady = okGl && webglSupported();
      if (!okGl) toast('echarts-gl 加载失败：真3D 视图已禁用（等距/俯视不受影响）', true);
      else if (!webglSupported()) toast('WebGL 不可用：真3D 视图已禁用（等距/俯视不受影响）', true);
      drawPalletFromCache();          // 若当前处于可改进视图则重画
    });
  });
});

/* WebGL 可用性探测（bar3D 硬依赖；RDP/无GPU环境返回 false） */
function webglSupported() {
  try {
    const c = document.createElement('canvas');
    return !!(window.WebGLRenderingContext &&
      (c.getContext('webgl') || c.getContext('experimental-webgl')));
  } catch (e) { return false; }
}

/* 真3D 可用性 = gl 库已加载 且 WebGL 可用（两者缺一即禁用该选项） */
function bar3DAvailable() { return glReady && webglSupported(); }

/* ---------------- 脚本加载器（多 CDN 依次回退） ---------------- */
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.async = true;
    s.onload = resolve; s.onerror = reject;
    document.head.appendChild(s);
  });
}
async function loadScripts(urls) {
  for (const u of urls) {
    try { await loadScript(u); return true; } catch (e) { /* 尝试下一CDN */ }
  }
  return false;
}

/* ---------------- 工具 ---------------- */
function fmtSim(sec) {
  const s = Math.floor(sec || 0);
  const hh = String(Math.floor(s / 3600)).padStart(2, '0');
  const mm = String(Math.floor(s % 3600 / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}
let toastTimer = null;
function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, 3500);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}
async function sendCommand(cmd, params) {
  try {
    const ret = await api('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd, params: params || {} }),
    });
    toast(ret.msg || (ret.ok ? '命令已执行' : '命令被拒绝'), !ret.ok);
    pollStatus();
  } catch (e) {
    toast('命令发送失败：' + e.message, true);
  }
}
function bindButtons() {
  $('btnStart').onclick     = () => sendCommand('start');
  $('btnPause').onclick     = () => sendCommand('pause');
  $('btnReset').onclick     = () => sendCommand('reset');
  $('btnDoorOpen').onclick  = () => sendCommand('door_open');
  $('btnDoorClose').onclick = () => sendCommand('door_close');
  $('btnOutbound').onclick  = () => sendCommand('outbound');
  $('btnEstop').onclick     = () => sendCommand('estop');
  // 班次3修改：MES 追溯查询按钮（回车同效）
  $('btnTrace').onclick     = runTrace;
  $('traceInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') runTrace(); });
  // 垛型面板视图切换：等距(默认·零依赖) → 真3D → 俯视 → 循环
  $('btnPalletView').onclick = () => {
    const order = bar3DAvailable() ? ['iso', '3d', 'top'] : ['iso', 'top'];
    const next = order[(order.indexOf(palletViewMode) + 1) % order.length];
    palletViewMode = next;
    palletDrawnMode = '';
    pallet3DSig = '';               // 手动切换后允许立即按新视图重建
    lastBoxesKey = '';              // 强制立即重绘
    const nameMap = { iso: '等距自绘', '3d': '真3D(bar3D)', top: '俯视散点' };
    $('btnPalletView').textContent = `视图: ${nameMap[next]}`;
    drawPalletFromCache();
    toast(`垛型已切换为${nameMap[next]}视图`);
  };
  $('speedSel').onchange    = (e) => sendCommand('set_speed', { speed: Number(e.target.value) });
  $('levelSel').onchange    = () => { lastLocationsJson = ''; pollLocations(); };
}

/* =====================================================================
   REST 轮询
==================================================================== */
let cachedFleet = null;
async function pollStatus() {
  let d;
  try { d = await api('/api/status'); } catch (e) { return; }
  if (!d.ok) return;
  // ---- 时钟/运行态 ----
  $('simClock').textContent = `t=${d.ts_sim.toFixed(1)}s`;
  $('runState').textContent = d.line_estop ? '急停锁存'
    : (d.clock.paused ? '已暂停' : '运行中');
  $('runState').className = 'badge ' +
    (d.line_estop ? 'state-estop' : (d.clock.paused ? 'state-pause' : 'state-run'));
  $('wsState').textContent = `WS ${hubCountText(d.ws_clients)}`;
  // ---- 流程图单元节点 ----
  renderFlow(d.units, d.agv_fleet, d.injector);
  renderDevTable(d.units, d.agv_fleet, d.injector);
  // ---- AGV 地图 ----
  cachedFleet = d.agv_fleet;
  drawAgvFromCache();
}
function hubCountText(n) { return n > 0 ? `已连 ${n} 端` : '无客户端'; }

function setNode(elId, state) {
  const el = $(elId);
  el.dataset.state = state;
  const em = el.querySelector('em');
  if (em) { em.textContent = state; em.style.color = STATE_COLOR[state] || '#fff'; }
}

function renderFlow(units, fleet, injector) {
  const a = units.assembly, v = units.vision,
        p = units.palletizer, w = units.warehouse;
  // 装配
  setNode('nodeAsm', a.state);
  $('asmStep').textContent = `步骤: ${a.step} ${a.step_progress}%`;
  $('asmProg').style.width = `${a.step_progress}%`;
  // 视觉
  setNode('nodeVis', v.state);
  $('visCnt').textContent = `OK ${v.ok} / NG ${v.ng}（NG率 ${(v.ng_rate * 100).toFixed(1)}%）`;
  $('visRework').textContent = v.rework_len;
  // 码垛
  setNode('nodePal', p.state);
  $('palFill').textContent = `当前垛 ${p.current_fill}`;
  $('palDone').textContent = p.pallets_done;
  // 立体库
  setNode('nodeWh', w.state);
  $('whStockTxt').textContent = `在库 ${w.stock}/${w.capacity}`;
  $('whInQ').textContent = w.in_queue;
  $('whStaging').textContent = w.staging;
  // 返修支线（有 NG 才点亮）
  const ngEl = $('nodeRework');
  ngEl.style.borderColor = v.rework_len > 0 ? '#ff5252' : '#37586f';
  $('reworkNum').textContent = v.rework_len;
  // 出货口与AGV（增强：显示当前垛进度，解释车队"间歇待命"是正常节拍；
  // 出货口节点随出库活动着色——有托盘待运/在途=运行绿，有出厂记录=待机黄）
  if (fleet) {
    $('agvBrief').textContent =
      `待派 ${fleet.pending} · 执行 ${fleet.active} · 完成 ${fleet.done['入库'] || 0}入/${fleet.done['出库'] || 0}出 · 当前垛${p.current_fill}`;
    const busy = fleet.pending + fleet.active;
    setNode('nodeAgv', busy > 0 ? '运行' : '待机');
    setNode('nodeShip', w.staging > 0 ? '运行' : (fleet.shipped > 0 ? '待机' : '停止'));
    $('shipNum').textContent = `已出厂 ${fleet.shipped} 托`;
  }
  // 生效中故障提示：把对应节点打上故障色（设备本身 state 已含故障）
  void injector;
}

function renderDevTable(units, fleet, injector) {
  const rows = [];
  const brief = (s, txt) => ({ s, txt });
  const a = units.assembly, v = units.vision, p = units.palletizer, w = units.warehouse;
  rows.push(brief(a, `${a.step} ${a.step_progress}% · 流出${a.products_out}件`));
  rows.push(brief(v, `OK${v.ok}/NG${v.ng} · 待检${v.queue_len}`));
  rows.push(brief(p, `${p.current_fill} · 完成托${p.pallets_done}`));
  rows.push(brief(w, `在库${w.stock}/${w.capacity} · 入${w.inbound_done}/出${w.outbound_done}`));
  if (fleet) for (const g of fleet.agvs) {
    rows.push({
      s: { id: g.id, name: g.name, state: g.state, fault: g.fault },
      txt: `相位:${g.phase} @(${g.pos[0]},${g.pos[1]})m 电量${g.battery}% 单数${g.tasks_done}`,
    });
  }
  const injActive = new Set((injector.active || []).map(f => f.dev));
  $('devBody').innerHTML = rows.map(({ s, txt }) => `
    <tr>
      <td>${s.id} ${s.name}</td>
      <td class="st-${s.state}">${s.state}${injActive.has(s.id) ? ' ⚠' : ''}</td>
      <td>${txt}</td>
      <td>${s.fault ? `<span style="color:#ff5252">${s.fault}</span>` : '—'}</td>
    </tr>`).join('');
}

/* ---------------- KPI 条 + 趋势 ---------------- */
let cachedTrend = [];
async function pollKpi() {
  let d;
  try { d = await api('/api/kpi'); } catch (e) { return; }
  if (!d.ok) return;
  const k = d.kpi;
  $('kOut').textContent = k.products_out;
  $('kOk').textContent = k.ok;
  $('kNg').textContent = k.ng;
  $('kNgRate').textContent = k.ng_rate_pct.toFixed(1);
  $('kBoxes').textContent = k.boxes_total;
  $('kPallets').textContent = k.pallets_done;
  $('kStock').textContent = k.stock;
  $('kShip').textContent = k.shipped;
  $('kFaults').textContent = k.faults_total;
  $('kAvail').textContent = k.availability.assembly;
  cachedTrend = d.trend || [];
  refreshTrendFromCache();
  // NG率仪表盘联动（仿真验证值）
  if (charts.gauge) {
    charts.gauge.setOption({
      series: [{ data: [{ value: k.ng_rate_pct, name: '累计 NG 率(仿真验证值)' }] }],
    });
  }
}
function refreshTrendFromCache() {
  if (!charts.trend || !cachedTrend.length) return;
  const xs = cachedTrend.map(b => fmtSim(b.t));
  charts.trend.setOption({
    xAxis: { data: xs },
    series: [
      { name: '装配流出', data: cachedTrend.map(b => b.out) },
      { name: '质检OK', data: cachedTrend.map(b => b.ok) },
      { name: '质检NG', data: cachedTrend.map(b => b.ng) },
      { name: '故障注入', data: cachedTrend.map(b => b.faults) },
    ],
  });
}

/* ---------------- 垛型 3D ---------------- */
let cachedPallet = null;
async function pollPallet() {
  let d;
  try { d = await api('/api/pallet3d'); } catch (e) { return; }
  if (!d.ok) return;
  cachedPallet = d;
  $('palletTitle').textContent =
    `当前垛 ${d.current_pallet_id} · ${d.grid.length}/${d.capacity}`;
  drawPalletFromCache();
}
/* ---------------- 垛型 3D/俯视（重建循环修复版） ----------------
   修复记录：原实现每次数据变化都 clear()+整段 setOption 重建 GL 场景——
   在软件渲染 WebGL（RDP/无GPU 环境）下单次重建耗时超过轮询间隔，
   画面长期停在未完成帧（看似空白）；只有垛满清零的空场景能瞬间画完，
   于是表现为"仅垛满时短暂出现坐标盒"；且每秒 clear() 持续打断视角，
   导致完全无法拖拽旋转。
   现改为 echarts 标准用法：
     a) 坐标轴/grid3D 等"静态骨架"只在模式切换(3d/2d)时设置一次；
     b) 数据更新只 merge series.data —— 不 clear、不重置视角，交互可保留；
     c) 3D 更新做 1.2s 节流，慢渲染环境下不再被高频重建淹没。 */
let palletDrawnMode = '';        // echarts 已绘制骨架标记：'' | '3d' | 'top'
let palletViewMode = 'iso';      // 面板④视图：'iso'(等距自绘,默认·零依赖) | '3d'(bar3D) | 'top'(俯视)
let palletIsoCanvas = null;      // 等距模式的自绘画布（纯 canvas 2D，任何环境必然可渲染）
let palletDomKind = '';          // DOM 载体现状：'' | 'iso' | 'echarts'（幂等切换的依据）
let pallet3DSig = '';            // 真3D 当前已渲染内容的签名（内容变化才 merge）
let palletSavedCam = null;       // 真3D 交互后的相机参数（alpha/beta/distance），更新时回填防视角重置
let lastPallet3DAt = 0;          // 上次真3D merge 的墙钟时刻（软渲染节流用）
let pallet3DColumns = [];        // 真3D 聚合后的列数据（悬浮提示经 dataIndex 回查）

/* 相机捕获：从 echarts 实例读回交互后的 viewControl（alpha/beta/distance），
   供真3D 重建时回填——否则每次 setOption 都会把视角打回默认值 */
function palletCaptureCam() {
  try {
    const vc = charts.pallet.getOption().grid3D[0].viewControl;
    if (vc && typeof vc.alpha === 'number') {
      palletSavedCam = { alpha: vc.alpha, beta: vc.beta,
                         distance: vc.distance,
                         center: Array.isArray(vc.center) ? vc.center.slice()
                                                          : [0, 0, 0] };
    }
  } catch (e) { /* 实例未就绪等场景：保留上一次的相机参数 */ }
}

function pallet3DBaseOption(cam) {
  const axLabel = { textStyle: { color: '#7fa3bf', fontSize: 11 } };
  return {
    tooltip: {
      formatter: (p) => {
        const c = pallet3DColumns[p.dataIndex];
        return c ? `${c.top.product_id}（顶层）<br>格(列,行)=(${c.x},${c.y})
                   <br>${c.layers} 层 · 高 ${c.layers * 180}mm` : '';
      },
    },
    // 修复记录：连续毫米轴 + barSize 数组在部分软渲染环境下不生效
    // （实测柱体仅约40mm宽、呈细针/金字塔状）。改用 bar3D 标准的分类轴用法：
    // 列(3)×行(4) 离散格位，柱体自动满格成规则立方块，形状由坐标系保证。
    xAxis3D: { type: 'category', name: '列', data: ['0', '1', '2'],
               axisLabel: axLabel },
    yAxis3D: { type: 'category', name: '行', data: ['0', '1', '2', '3'],
               axisLabel: axLabel },
    zAxis3D: { name: '高度(mm)', min: 0, max: 800, axisLabel: axLabel },
    grid3D: Object.assign({
      boxWidth: 60, boxDepth: 80, boxHeight: 80,
      light: { main: { intensity: 1.1 }, ambient: { intensity: .35 } },
    }, cam ? { viewControl: Object.assign({ distance: 260, alpha: 28, beta: 40 },
                                           cam) }
           : { viewControl: { distance: 260, alpha: 28, beta: 40 } }),
    series: [{
      type: 'bar3D', data: [],
      // 修复记录：本环境对任何显式 barSize 都会生成针状坏网格（mm轴230与分类轴0.85
      // 均实测为细针）——故不设置 barSize，采用自动满格；配合按列聚合消除嵌套后，
      // 全场景无重叠几何，共面接缝在不透明渲染下不再可见。
      // 不用半透明：透明混合会放大相邻柱接缝的闪烁（穿模观感来源之一）
      itemStyle: { opacity: 1 }, shading: 'color',
    }],
  };
}

/* ---- 等距投影自绘（面板④默认视图）：纯 canvas 2D，零 GL 依赖，任何环境必然可渲染。
   修复记录：bar3D 在用户环境（软渲染 WebGL）下网格静默不渲染——坐标盒/交互正常但
   柱体消失，换版本/换着色均无效。故默认改用自绘等距立方体：按 (x+y+z) 画家算法
   排序、顶/右/左三面明暗着色，视觉等效 3D 且与已验证可用的俯视散点同技术底座。 ---- */
const PALLET_LAYER_COLORS = ['#63b3ff', '#27d68f', '#ffd54f', '#ff9f43'];

function shadeHex(hex, f) {
  // 颜色明暗缩放：f>1 变亮，f<1 变暗（用于立方体三个面的伪光照）
  const n = parseInt(hex.slice(1), 16);
  const ch = (v) => Math.max(0, Math.min(255, Math.round(v * f)));
  const r = ch((n >> 16) & 255), g = ch((n >> 8) & 255), b = ch(n & 255);
  return `rgb(${r},${g},${b})`;
}

function ensurePalletIsoCanvas() {
  const host = $('chartPallet');
  // 竞态修复：echarts 脚本可能尚未加载完成，此时不得访问全局 echarts 对象
  const inst = (window.echarts ? echarts.getInstanceByDom(host) : null);
  if (inst) { inst.dispose(); charts.pallet = null; }
  if (!palletIsoCanvas) {
    palletIsoCanvas = document.createElement('canvas');
    palletIsoCanvas.style.width = '100%';
    palletIsoCanvas.style.height = '100%';
  }
  if (palletIsoCanvas.parentNode !== host) host.appendChild(palletIsoCanvas);
}

function leavePalletIsoCanvas() {
  if (palletIsoCanvas && palletIsoCanvas.parentNode) palletIsoCanvas.remove();
  // 竞态修复：echarts 未就绪时只清画布、不建实例（等 loadScripts 完成后按需创建）
  if (!window.echarts) { charts.pallet = null; return; }
  const host = $('chartPallet');
  const inst = echarts.getInstanceByDom(host);
  if (!inst) charts.pallet = echarts.init(host);
  else charts.pallet = inst;
  palletDrawnMode = '';         // 骨架需重设
}

function drawPalletIsoCanvas(grid) {
  ensurePalletIsoCanvas();
  const cv = palletIsoCanvas;
  const host = $('chartPallet');
  const W = host.clientWidth || 300, H = host.clientHeight || 220;
  const dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const dims = (cachedPallet && cachedPallet.dims) || [3, 4, 4];
  const DX = dims[0], DY = dims[1], DZ = dims[2];
  // 等距基向量：x轴右下、y轴左下、z轴垂直向上；a 为单格屏幕尺寸（自动适配容器）
  const a = Math.min(W * 0.86 / Math.max(DX + DY - 2, 1),
                     H * 0.88 / ((DX + DY - 2) * 0.5 + DZ * 0.75));
  const bx = a, by = a * 0.5, cz = a * 0.75;
  const ox = W * 0.5 - (DX - DY) * a / 2;                // 水平居中（足迹中心对齐）
  const oy = H * 0.94;
  const proj = (x, y, z) => [ox + (x - y) * bx,
                             oy - (x + y) * by - z * cz];

  // 底座平台（垛型足迹），空垛时也有内容可见
  ctx.beginPath();
  let p00 = proj(-0.06, -0.06, 0), p10 = proj(DX + 0.06, -0.06, 0),
      p11 = proj(DX + 0.06, DY + 0.06, 0), p01 = proj(-0.06, DY + 0.06, 0);
  ctx.moveTo(p00[0], p00[1]); ctx.lineTo(p10[0], p10[1]);
  ctx.lineTo(p11[0], p11[1]); ctx.lineTo(p01[0], p01[1]); ctx.closePath();
  ctx.fillStyle = '#123049'; ctx.fill();
  ctx.strokeStyle = '#1d3247'; ctx.stroke();

  // 立方体按画家算法排序（修复记录：原按 (x+y+z) 升序=近先远后，方向反了且 z 权重
  // 错误，导致内部相邻箱体互相错盖——只有轮廓边缘与顶层幸存）。本投影视线方向
  // d∝(1,1,-4/3)（由 b=a/2、c=0.75a 推得），深度键=x+y-1.5z，远(键大)者先画。
  const cells = [...grid].sort((u, v) =>
    (v.x + v.y - v.z * 1.5) - (u.x + u.y - u.z * 1.5));
  for (const b of cells) {
    const col = PALLET_LAYER_COLORS[b.z % PALLET_LAYER_COLORS.length];
    const t0 = proj(b.x, b.y, b.z + 1);          // 顶面·前角
    const tx = proj(b.x + 1, b.y, b.z + 1);      // 顶面·右角
    const ty = proj(b.x, b.y + 1, b.z + 1);      // 顶面·左角
    const txy = proj(b.x + 1, b.y + 1, b.z + 1); // 顶面·后角
    // 面数修复：本投影(sx=(x-y)a, sy=-(x+y)b-zc)下朝向观察者的是 -y 与 -x 两面墙
    // （屏幕法线朝下），原实现误画 +x/+y 背面墙——表现为立方体缺两个正面。
    const f00 = proj(b.x, b.y, b.z);             // 前下角(共享底点)
    const fx0 = proj(b.x + 1, b.y, b.z);         // -y 墙·底右
    const fy0 = proj(b.x, b.y + 1, b.z);         // -x 墙·底左
    // -y 墙（右前面）：f00 → fx0 → tx → t0
    ctx.beginPath();
    ctx.moveTo(f00[0], f00[1]); ctx.lineTo(fx0[0], fx0[1]);
    ctx.lineTo(tx[0], tx[1]); ctx.lineTo(t0[0], t0[1]); ctx.closePath();
    ctx.fillStyle = shadeHex(col, 0.62); ctx.fill();
    ctx.strokeStyle = 'rgba(11,22,32,.55)'; ctx.lineWidth = 1; ctx.stroke();
    // -x 墙（左前面）：f00 → fy0 → ty → t0
    ctx.beginPath();
    ctx.moveTo(f00[0], f00[1]); ctx.lineTo(fy0[0], fy0[1]);
    ctx.lineTo(ty[0], ty[1]); ctx.lineTo(t0[0], t0[1]); ctx.closePath();
    ctx.fillStyle = shadeHex(col, 0.42); ctx.fill(); ctx.stroke();
    // 顶面（最亮，最后画避免被墙面盖住边缘）
    ctx.beginPath();
    ctx.moveTo(t0[0], t0[1]); ctx.lineTo(tx[0], tx[1]);
    ctx.lineTo(txy[0], txy[1]); ctx.lineTo(ty[0], ty[1]); ctx.closePath();
    ctx.fillStyle = shadeHex(col, 1.18); ctx.fill(); ctx.stroke();
  }

  if (!grid.length) {
    ctx.fillStyle = '#7fa3bf'; ctx.font = '12px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('等待码箱…（等距视图 · canvas 自绘）', W / 2, H * 0.16);
  }
}

/* 视图模式切换（幂等修复版）：只有 iso↔echarts 真正换族时才动 DOM/重置骨架。
   修复记录：原实现每次轮询都无条件走 ensure/leave，而 leave 会把 palletDrawnMode
   清空——导致真3D 每秒都被判"骨架需重设"而 clear+全量重建，软渲染 GL 直接冻结在坏帧。 */
function setPalletDomMode(mode) {
  const want = (mode === 'iso') ? 'iso' : 'echarts';
  if (palletDomKind === want) {
    // 同族内：仅补挂可能缺失的实例引用，绝不动骨架/画布
    if (want === 'echarts' && !charts.pallet && window.echarts) {
      const host = $('chartPallet');
      charts.pallet = echarts.getInstanceByDom(host) || echarts.init(host);
      palletDrawnMode = '';
    }
    return;
  }
  if (want === 'iso') ensurePalletIsoCanvas();
  else leavePalletIsoCanvas();
  palletDomKind = want;
}

function drawPalletFromCache() {
  if (!cachedPallet) return;
  // 满托换盘无缝衔接：当前垛清空(输出期)时改显示刚完成的垛档案，
  // 消除"堆满一瞬间只剩空白盒"的观感断裂
  const doneBoxes = (cachedPallet.last_completed && cachedPallet.last_completed.boxes) || [];
  const grid = cachedPallet.grid.length ? cachedPallet.grid : doneBoxes;
  const key = (cachedPallet.grid.length ? 'C' : 'D') + grid.length + '@'
            + cachedPallet.current_pallet_id;
  if (key === lastBoxesKey) return;

  // 真3D 仅在 gl 库+WebGL 双就绪时可达；异常组合防御性回退等距（规格4⑦语义修正）
  const mode = (palletViewMode === '3d' && !bar3DAvailable()) ? 'iso' : palletViewMode;

  if (mode === 'iso') {                          // 默认：canvas 自绘等距图
    setPalletDomMode('iso');
    lastBoxesKey = key;
    drawPalletIsoCanvas(grid);
    return;
  }

  // 先确保 DOM/实例与目标模式匹配（echarts 未就绪时保持空实例，不抛错）
  setPalletDomMode(mode);
  if (!charts.pallet) return;                    // echarts 尚未加载：键不落账，下轮重试
  lastBoxesKey = key;

  if (mode === '3d') {
    /* 真3D 实时生长（设计变更：应用户要求恢复逐箱可见）：
       DOM 切换已幂等化，此处只剩纯 series 数据 merge——不再有 clear 全量重建，
       软渲染竞态的源头已移除；配合相机回填与 700ms 节流保证视角稳定、刷新平滑。
       换垛清零瞬间由上层 grid 回退到 last_completed 显示成品，不闪空白。 */
    const nowMs = Date.now();
    if (nowMs - lastPallet3DAt < 700) return;    // 节流：键未落账，下轮重试
    lastPallet3DAt = nowMs;
    const sig = 'G:' + grid.length + '@' + cachedPallet.current_pallet_id;
    if (sig === pallet3DSig) return;             // 内容未变化
    pallet3DSig = sig;
    palletCaptureCam();                          // 记下用户当前视角

    if (palletDrawnMode !== '3d') {
      charts.pallet.clear();
      charts.pallet.setOption(pallet3DBaseOption(palletSavedCam));
      palletDrawnMode = '3d';
    }
    charts.pallet.setOption({
      grid3D: { viewControl: Object.assign({}, palletSavedCam || {}) },
      series: [{
        /* 穿模根治：原映射把同列多层箱画成多根从地面长起的嵌套柱（几何完全重叠、
           侧壁共面→必然深度冲突）。现按 (列,行) 聚合为单柱，高度=最高层×180，
           全场景无任何重叠/嵌套几何（柱体自动满格，不设 barSize）。 */
        data: (() => {
          const cols = new Map();
          for (const b of grid) {
            const k = b.x + ',' + b.y;
            const prev = cols.get(k);
            if (!prev || (b.z + 1) > prev.layers) {
              cols.set(k, { x: b.x, y: b.y, layers: b.z + 1, top: b });
            }
          }
          pallet3DColumns = [...cols.values()];
          return pallet3DColumns.map(c => ({
            value: [String(c.x), String(c.y), c.layers * 180],
            itemStyle: { color: PALLET_LAYER_COLORS[(c.layers - 1) % 4] },
          }));
        })(),
      }],
    });                                          // 相机参数随更新一并回填
  } else {                                       // 'top' 俯视散点
    if (palletDrawnMode !== 'top') {
      charts.pallet.clear();
      palletDrawnMode = 'top';
    }
    charts.pallet.setOption({
      title: { text: '俯视图', left: 'center',
               textStyle: { color: '#7fa3bf', fontSize: 11 } },
      tooltip: { formatter: (p) => {
        const b = grid[p.dataIndex];
        return b ? `${b.product_id}<br>层Z=${b.z} 格=(${b.x},${b.y})` : ''; } },
      xAxis: { name: 'X(mm)', min: -350, max: 350 },
      yAxis: { name: 'Y(mm)', min: -480, max: 480 },
      // 增强：俯视图挂 inside 数据缩放——滚轮缩放、按住拖拽平移
      dataZoom: [{ type: 'inside', xAxisIndex: 0, filterMode: 'none' },
                 { type: 'inside', yAxisIndex: 0, filterMode: 'none' }],
      series: [{
        type: 'scatter', symbolSize: 26,
        data: grid.map(b => ({
          value: [b.px_mm, b.py_mm],
          itemStyle: { color: PALLET_LAYER_COLORS[b.z % 4] },
        })),
      }],
    });
  }
}

/* ---------------- 库位热力图 ---------------- */
async function pollLocations() {
  let d;
  try { d = await api('/api/warehouse/locations'); } catch (e) { return; }
  if (!d.ok) return;
  const sig = JSON.stringify([d.stock, $('levelSel').value]);
  if (sig === lastLocationsJson) return;         // 无变化不重绘
  lastLocationsJson = sig;
  if (!charts.heat) return;
  const lvl = Number($('levelSel').value);        // 0=汇总(各层叠加)
  // 按 (排row, 列bay) 聚合占用数
  const agg = {};
  for (const loc of d.locations) {
    const k = `${loc.row}-${loc.bay}`;
    if (!(k in agg)) agg[k] = 0;
    if (lvl === 0 ? loc.occupied : (loc.level === lvl && loc.occupied)) agg[k] += 1;
  }
  const cells = [];
  for (let r = 1; r <= d.rows; r++)
    for (let c = 1; c <= d.bays; c++)
      cells.push([c - 1, d.rows - r, agg[`${r}-${c}`] || 0]);   // 排倒序让A排在上方
  const maxV = lvl === 0 ? d.levels : 1;
  charts.heat.setOption({
    visualMap: { max: maxV },
    series: [{ data: cells }],
  });
  document.querySelector('#chartHeat').parentElement
    .querySelector('.sub').textContent =
    `${lvl === 0 ? '各层叠加(0-' + d.levels + ')' : '第' + lvl + '层(0/1)'} · 在库 ${d.stock}/${d.capacity}`;
}

/* =====================================================================
   WebSocket 实时事件
==================================================================== */
function connectWS() {
  const url = `ws://${location.hostname}:${WS_PORT}/ws`;
  try { ws = new WebSocket(url); } catch (e) { fallbackPollEvents(); return; }
  ws.onopen = () => {
    wsRetry = 0;
    $('evtSrc').textContent = 'WebSocket 实时推送中…';
    $('wsState').className = 'badge ws-on';
  };
  ws.onmessage = (m) => {
    try {
      const msg = JSON.parse(m.data);
      if (msg.kind === 'event') appendEvent(msg.event);
    } catch (e) { /* 忽略坏帧 */ }
  };
  ws.onclose = ws.onerror = () => {
    $('wsState').className = 'badge ws-off';
    wsRetry += 1;
    if (wsRetry <= 3) setTimeout(connectWS, 2500);
    else fallbackPollEvents();               // 降级：REST 轮询兜底
  };
}
function fallbackPollEvents() {
  if (wsFallbackTimer) return;
  $('evtSrc').textContent = 'WS不可用，已降级为 REST 轮询(2s)';
  wsFallbackTimer = setInterval(async () => {
    try {
      const d = await api('/api/events?n=30');
      if (!d.ok) return;
      for (const ev of d.events) appendEvent(ev, true);
    } catch (e) { /* 服务未起 */ }
  }, 2000);
}

/* ---------------- 事件表渲染 ---------------- */
function summarize(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case 'device.state':      return `${ev.source} → ${d.state}（${d.reason || ''}）`;
    case 'fault.raised':      return `[${d.origin}] ${d.fault_type}`;
    case 'fault.cleared':     return `${d.fault_type} 已复位（历时${d.duration_s}s）`;
    case 'flow.product_out':  return `${(d.product || {}).product_id} 流出（节拍${d.takt_s}s）`;
    case 'vision.ok':
    case 'vision.ng':         return `${d.product_id} 判定${d.result} 尺寸=${d.dim_mm}mm`;
    case 'pallet.box_placed': return `${(d.product_id || '')} → 格(${d.x},${d.y},${d.z})`;
    case 'pallet.full':       return `${d.pallet_id} 满 ${d.box_count} 箱`;
    case 'agv.call':          return `${d.pallet_id} 从${d.from}去${d.to}`;
    case 'agv.task_created':  return `${d.task_id} ${d.task_type} ${d.pallet_id}: ${d.from_station}→${d.to_station}`;
    case 'agv.phase':         return `${d.agv_id} ${d.prev_phase}→${d.phase}`;
    case 'agv.task_done':     return `${d.task_id} ${d.task_type} ${d.pallet_id} 交付完成`;
    case 'wh.inbound_done':   return `${d.pallet_id} 上架 ${d.loc_id}（在库${d.stock}）`;
    case 'wh.outbound_done':  return `${d.pallet_id} 下架（在库${d.stock}）`;
    case 'ui.command':        return `屏幕命令: ${d.cmd} ${JSON.stringify(d.params || {})}`;
    // 班次3修改：MES/EMS 事件摘要
    case 'mes.order_created': return `开立 ${d.wo_id} 型号${d.model} 计划${d.target_qty}件`;
    case 'mes.order_closed':  return `${d.wo_id} 完工关单（OK${d.ok}/NG${d.ng}）`;
    case 'ems.health_alert':  return `${d.dev_id} 健康分 ${d.score}：${d.advice}`;
    case 'ems.maintenance':   return `${d.dev_id} 维护动作（${d.reason}）`;
    default:
      return Object.keys(d).length ? JSON.stringify(d) : '';
  }
}
function appendEvent(ev, dedupe) {
  if (dedupe && ev.seq <= evSeqMax) return;    // 降级轮询按 seq 去重
  evSeqMax = Math.max(evSeqMax, ev.seq);
  const tb = $('evBody');
  const tr = document.createElement('tr');
  tr.className = 'ev-new';
  const sev = (ev.severity || 'INFO').toUpperCase();
  tr.innerHTML = `
    <td>${ev.seq}</td>
    <td>${ev.ts_sim.toFixed(1)}</td>
    <td>${ev.source}</td>
    <td>${EVT_LABEL[ev.type] || ev.type}</td>
    <td class="t-${sev.toLowerCase()}">${sev}</td>
    <td>${summarize(ev)}</td>`;
  tb.insertBefore(tr, tb.firstChild);
  while (tb.rows.length > 100) tb.deleteRow(-1);
}

/* =====================================================================
   图表初始化（ECharts 就绪后调用）
==================================================================== */
function initCharts() {
  const axisStyle = {
    axisLine: { lineStyle: { color: '#2c4a66' } },
    axisLabel: { color: '#7fa3bf', fontSize: 10 },
    splitLine: { lineStyle: { color: 'rgba(44,74,102,.4)' } },
  };
  /* ---- ② 趋势折线 ---- */
  charts.trend = echarts.init($('chartTrend'));
  charts.trend.setOption({
    backgroundColor: 'transparent',
    color: ['#29b6f6', '#00e676', '#ff5252', '#ffd54f'],
    tooltip: { trigger: 'axis' },
    legend: { data: ['装配流出', '质检OK', '质检NG', '故障注入'],
              textStyle: { color: '#7fa3bf', fontSize: 10 }, top: 0 },
    grid: { left: 34, right: 12, top: 28, bottom: 22 },
    xAxis: Object.assign({ type: 'category', data: [] }, axisStyle),
    yAxis: Object.assign({ type: 'value', minInterval: 1 }, axisStyle),
    series: [
      { name: '装配流出', type: 'line', smooth: true, showSymbol: false, areaStyle: { opacity: .12 } },
      { name: '质检OK', type: 'line', smooth: true, showSymbol: false },
      { name: '质检NG', type: 'line', smooth: true, showSymbol: false },
      { name: '故障注入', type: 'bar', barWidth: 8 },
    ],
  });

  /* ---- ③ NG率仪表盘 ---- */
  charts.gauge = echarts.init($('chartGauge'));
  charts.gauge.setOption({
    backgroundColor: 'transparent',
    series: [{
      type: 'gauge', min: 0, max: 15,
      startAngle: 210, endAngle: -30,
      progress: { show: true, width: 14, itemStyle: { color: '#ffd54f' } },
      axisLine: { lineStyle: { width: 14, color: [[1, '#1d3247']] } },
      axisTick: { distance: -20 },
      splitLine: { length: 8, distance: -24, lineStyle: { color: '#7fa3bf' } },
      axisLabel: { color: '#7fa3bf', distance: 18, fontSize: 9 },
      pointer: { itemStyle: { color: '#ffd54f' } },
      anchor: { show: true, size: 12 },
      detail: {
        valueAnimation: true, formatter: '{value}%',
        color: '#ffd54f', fontSize: 26, offsetCenter: [0, '62%'],
      },
      title: { offsetCenter: [0, '88%'], color: '#7fa3bf', fontSize: 11 },
      data: [{ value: 0, name: '累计 NG 率(仿真验证值)' }],
    }],
  });

  /* ---- ④ 垛型：默认等距自绘(canvas)不占 echarts 实例；
         切到 真3D/俯视 时由 setPalletDomMode 按需初始化 ---- */

  /* ---- ⑤ 库位热力图（4排×10列）---- */
  charts.heat = echarts.init($('chartHeat'));
  charts.heat.setOption({
    backgroundColor: 'transparent',
    tooltip: { position: 'top',
      formatter: (p) => `${String.fromCharCode(65 + (3 - p.data[1]))}排 第${p.data[0] + 1}列：占用 ${p.data[2]}` },
    grid: { left: 46, right: 16, top: 16, bottom: 42 },
    xAxis: Object.assign({ type: 'category', name: '列',
      data: Array.from({ length: 10 }, (_, i) => i + 1) }, axisStyle),
    yAxis: Object.assign({ type: 'category', name: '排',
      data: ['D排', 'C排', 'B排', 'A排'] }, axisStyle),
    visualMap: {
      min: 0, max: 5, calculable: false, orient: 'horizontal',
      left: 'center', bottom: 0, itemHeight: 60, itemWidth: 12,
      inRange: { color: ['#123049', '#1b6b9a', '#27d68f', '#ffd54f', '#ff9f43', '#ff5252'] },
      textStyle: { color: '#7fa3bf', fontSize: 9 },
    },
    series: [{
      type: 'heatmap', data: [],
      label: { show: true, color: '#cfe8ff', fontSize: 9 },
      itemStyle: { borderColor: '#0b1620', borderWidth: 1 },
    }],
  });

  /* ---- ⑥ AGV 物流地图 ---- */
  charts.agv = echarts.init($('chartAgv'));
  const stn = (n, x, y) => ({ name: n, value: [x, y] });
  charts.agv.setOption({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    grid: { left: 36, right: 20, top: 20, bottom: 30 },
    xAxis: Object.assign({ type: 'value', name: 'x(m)', min: 0, max: 20 }, axisStyle),
    yAxis: Object.assign({ type: 'value', name: 'y(m)', min: 0, max: 10 }, axisStyle),
    series: [
      { type: 'lines', coordinateSystem: 'cartesian2d', polyline: false,
        data: [
          { coords: [[6, 2], [12, 2]] },      // 入库运输道 PAL-OUT→WH-IN
          { coords: [[12, 6], [18, 6]] },     // 出库运输道 WH-OUT→SHIP
          { coords: [[3, 4.5], [6, 2]] },     // 待命位→码垛出口
          { coords: [[3, 7.5], [12, 6]] },    // 待命位→库出口
        ],
        lineStyle: { color: 'rgba(63,167,255,.35)', width: 3, curveness: 0, type: 'dashed' },
        silent: true },
      { type: 'scatter', name: '站点', symbolSize: 16,
        itemStyle: { color: '#1b6b9a', borderColor: '#63b3ff', borderWidth: 2 },
        label: { show: true, position: 'top', color: '#7fa3bf', fontSize: 10,
                 formatter: (p) => p.name },
        data: [stn('码垛出口', 6, 2), stn('库入口', 12, 2),
               stn('库出口', 12, 6), stn('出货口', 18, 6),
               stn('待命1', 3, 4.5), stn('待命2', 3, 7.5)] },
      { type: 'scatter', name: 'AGV-01', symbolSize: 26,
        itemStyle: { color: '#26c6da', shadowBlur: 12, shadowColor: '#26c6da' },
        label: { show: true, position: 'bottom', color: '#26c6da', fontSize: 10 },
        data: [] },
      { type: 'scatter', name: 'AGV-02', symbolSize: 26,
        itemStyle: { color: '#ffa726', shadowBlur: 12, shadowColor: '#ffa726' },
        label: { show: true, position: 'bottom', color: '#ffa726', fontSize: 10 },
        data: [] },
    ],
  });

  /* ---- ⑩ 能耗横向条形图（班次3修改：/api/ems/energy 数据源）---- */
  charts.energy = echarts.init($('chartEnergy'));
  charts.energy.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const p = ps[0];
        const d = energyCache.devices[p.dataIndex];
        return d ? `${d.name}（${d.id}）<br>累计 ${d.kwh} kWh · 当前 ${d.kw_now} kW` : '';
      },
    },
    grid: { left: 70, right: 46, top: 8, bottom: 20 },
    xAxis: Object.assign({ type: 'value', name: 'kWh' }, axisStyle),
    yAxis: Object.assign({ type: 'category',
      data: [] }, axisStyle),
    series: [{
      type: 'bar', barWidth: 10,
      itemStyle: { color: '#26c6da', borderRadius: 4,
                   shadowBlur: 6, shadowColor: 'rgba(38,198,218,.5)' },
      label: { show: true, position: 'right', color: '#7fa3bf', fontSize: 9,
               formatter: (p) => `${p.value}` },
      data: [],
    }],
  });

  window.addEventListener('resize', () => {
    for (const c of Object.values(charts)) c && c.resize();
    // 等距自绘画布随容器尺寸重投影
    if (palletViewMode === 'iso' && cachedPallet) drawPalletIsoCanvas(cachedPallet.grid);
  });
}

/* ---------------- AGV 地图刷新（来自 /api/status 缓存） ---------------- */
function drawAgvFromCache() {
  if (!charts.agv || !cachedFleet) return;
  const series = [];
  for (let i = 0; i < 2; i++) {
    const g = cachedFleet.agvs[i];
    series.push(g ? [{
      value: g.pos,
      name: `${g.id} ${g.phase}`,
      label: { formatter: `{a|${g.id}}\n{b|${g.phase}}`,
               rich: { a: { color: '#cfe8ff', fontSize: 10 },
                       b: { color: '#7fa3bf', fontSize: 9 } } },
    }] : []);
  }
  charts.agv.setOption({ series: [
    { /* lines 保持 */ },
    { /* stations 保持 */ },
    { name: 'AGV-01', data: series[0] },
    { name: 'AGV-02', data: series[1] },
  ]});
  $('agvMapSub').textContent =
    `${cachedFleet.agv_count} 台车 · 待派${cachedFleet.pending} · 执行${cachedFleet.active} · 已出厂${cachedFleet.shipped}托`;
}

/* =====================================================================
   班次3修改：MES 工单面板（/api/mes/orders + /api/mes/trace）
==================================================================== */
let energyCache = { devices: [] };      // 能耗面板缓存（图表 tooltip 用）

async function pollMes() {
  let d;
  try { d = await api('/api/mes/orders'); } catch (e) { return; }
  if (!d.ok) return;
  const rep = d.report;
  // ---- 报工指标 chips ----
  if (rep) {
    $('mesChips').innerHTML = `
      <span class="mes-chip">报工产量<b>${rep.judged}</b>件</span>
      <span class="mes-chip">良品率<b class="${rep.quality_pct >= 95 ? 'good' : 'warn'}">${rep.quality_pct}%</b></span>
      <span class="mes-chip">可用率<b>${rep.availability_pct}%</b></span>
      <span class="mes-chip">性能率<b>${rep.performance_pct}%</b></span>
      <span class="mes-chip">OEE≈<b class="${rep.oee_pct >= 60 ? 'good' : 'warn'}">${rep.oee_pct}%</b></span>
      <span class="mes-chip">满托<b>${rep.pallets_done}</b>/出厂<b>${rep.shipped}</b>托</span>`;
  }
  // ---- 工单表 ----
  $('mesOrderBody').innerHTML = (d.orders || []).map((o) => `
    <tr>
      <td>${o.wo_id}</td>
      <td>${o.model}</td>
      <td><span class="prog-mini"><i style="width:${o.progress_pct}%"></i></span>${o.total}/${o.target_qty}</td>
      <td>${o.ok}/${o.ng}</td>
      <td>${o.yield_pct}%</td>
      <td class="${o.status === '已完成' ? 'wo-done' : ''}">${o.status}</td>
    </tr>`).join('');
}

/* ---------------- 追溯查询 ---------------- */
async function runTrace() {
  const q = ($('traceInput').value || '').trim();
  if (!q) { toast('请先输入产品号或托盘号', true); return; }
  let d;
  try {
    d = await api('/api/mes/trace?query=' + encodeURIComponent(q));
  } catch (e) { toast('追溯请求失败：' + e.message, true); return; }
  const box = $('traceResult');
  if (!d.ok) { box.textContent = `✘ ${d.msg}`; return; }
  const c = d.chain;
  const lines = [];
  lines.push(`✔ 命中【${d.kind}】 当前状态: ${d.status}`);
  if (d.kind === '产品') {
    lines.push(`产品 ${c.product.product_id} → 质检 ${c.product.result || '?'} @ t=${(c.product.ts_sim ?? '-')}s`);
    lines.push(`托盘 ${c.pallet_id} → 批次 ${c.batch_id || '?'} → 工单 ${c.wo_id || '?'}`);
    lines.push(`库位: ${c.location || '（不在库）'}`);
    for (const [ts, stage, note] of (c.pallet_events || [])) {
      lines.push(`  t=${ts}s  ${stage}  ${note}`);
    }
  } else {
    lines.push(`托盘 ${c.pallet_id} → 批次 ${c.batch_id || '?'} → 工单 ${c.wo_id || '?'}`);
    lines.push(`产品清单(${(c.products || []).length}): ${(c.products || []).slice(0, 8).join(', ')}${(c.products || []).length > 8 ? ' …' : ''}`);
    for (const [ts, stage, note] of (c.events || [])) {
      lines.push(`  t=${ts}s  ${stage}  ${note}`);
    }
  }
  box.textContent = lines.join('\n');
}

/* =====================================================================
   班次3修改：EMS 能耗/健康面板（/api/ems/energy + /api/ems/health）
==================================================================== */
async function pollEms() {
  await Promise.all([pollEnergy(), pollHealth()]);
}

async function pollEnergy() {
  let d;
  try { d = await api('/api/ems/energy'); } catch (e) { return; }
  if (!d.ok) return;
  energyCache = d;
  if (!d.enabled) { $('emsSub').textContent = 'EMS 未启用'; return; }
  // 班次3增强：分时电价——显示当前所处档位与单价
  const tou = (d.tou_enabled && d.period_now)
    ? ` · 现行[${d.period_now.tier}] ${d.period_now.price} 元/kWh` : '';
  $('emsSub').textContent =
    `合计 ${d.total_kwh} kWh · 电费≈${d.cost_yuan}元${tou} · CO₂≈${d.co2_kg}kg（仿真验证值）`;
  if (charts.energy) {
    const devs = [...(d.devices || [])].sort((a, b) => a.kwh - b.kwh);
    charts.energy.setOption({
      yAxis: { data: devs.map(x => `${x.id}`) },
      series: [{ data: devs.map(x => x.kwh) }],
    });
  }
}

async function pollHealth() {
  let d;
  try { d = await api('/api/ems/health'); } catch (e) { return; }
  if (!d.ok) return;
  const cls = (s) => s >= 80 ? 'hscore-good' : (s >= 60 ? 'hscore-mid' : 'hscore-bad');
  $('healthBody').innerHTML = (d.devices || []).map((x) => `
    <tr>
      <td>${x.dev_id} ${x.name}</td>
      <td class="${cls(x.score)}">${x.score} ${x.grade}</td>
      <td>${x.faults}次 / ${x.downtime_s}s</td>
      <td>${x.advice}${x.state === '维护' ? '（维护中）' : ''}</td>
    </tr>`).join('');
}
