# -*- coding: utf-8 -*-
"""
core/sim_clock.py —— 仿真时钟引擎（全厂唯一时间源）
===================================================
设计要点：
    1. 所有设备、回路、注入器一律通过 clock.now() 取仿真时间，
       禁止直接调用 time.time()，保证"加速跑批结果一致"这一硬性要求；
    2. 双运行模式：
       - 实时模式 start()：后台线程按 speed 倍率推进（1x/10x/60x），
         每推进一个 dt 就回调一次 step 回调（即全厂 update）；
       - 加速/批量模式 run_until(end)：同步循环满速推进，用于自检冒烟
         与未来跑批实验（不睡眠、确定性步数）；
    3. 支持暂停 pause()/恢复 resume()，两种模式下语义一致；
    4. 线程安全：now/set_speed/pause 等用锁保护。

假设记录：
    - 固定步长推进（非事件驱动插值），最稳妥且与 PLC 扫描周期心智模型一致。
"""

import threading
import time as _wall_time   # 仅用于实时模式的墙钟节拍，仿真时间绝不取自它
from typing import Callable, Optional


class SimClock:
    """统一仿真时间源。dt 为固定仿真步长；speed 为加速倍率（正数）。"""

    def __init__(self, dt: float = 0.1, speed: float = 10):
        self._dt = float(dt)                 # 固定仿真步长（秒）
        self._speed = float(speed)           # 当前加速倍率
        self._sim_now = 0.0                  # 当前仿真时间（秒），单调递增
        self._paused = False                 # 暂停标志
        self._running = False                # 实时模式线程运行标志
        self._lock = threading.RLock()       # 保护时间与状态的锁
        self._step_cb: Optional[Callable[[float], None]] = None  # 每 tick 的全厂步进回调
        self._thread: Optional[threading.Thread] = None
        # 统计：已执行 tick 数（供自检核对确定性）
        self._tick_count = 0

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def dt(self) -> float:
        """固定仿真步长（秒）。"""
        return self._dt

    @property
    def speed(self) -> float:
        """当前加速倍率。"""
        with self._lock:
            return self._speed

    @property
    def tick_count(self) -> int:
        """累计推进的 tick 数（诊断用）。"""
        with self._lock:
            return self._tick_count

    def is_paused(self) -> bool:
        """是否处于暂停状态。"""
        with self._lock:
            return self._paused

    def now(self) -> float:
        """当前仿真时间（秒）。全厂唯一的取时入口，四舍五入到毫秒便于显示。"""
        with self._lock:
            return round(self._sim_now, 3)

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------
    def set_speed(self, speed: float) -> None:
        """设置加速倍率（须为正数；推荐使用 1/10/60 预设）。"""
        v = float(speed)
        if v <= 0:
            raise ValueError("加速倍率必须为正数")
        with self._lock:
            self._speed = v

    def pause(self) -> None:
        """暂停仿真推进（实时线程挂起；run_until 调用前应先恢复）。"""
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        """从暂停中恢复推进。"""
        with self._lock:
            self._paused = False

    def set_step_callback(self, cb: Callable[[float], None]) -> None:
        """
        注册每 tick 的步进回调 cb(dt_sim)。
        实时模式下由时钟线程调用；run_until 未显式传参时也复用它。
        这样保证两种模式的推进路径完全一致。
        """
        self._step_cb = cb

    # ------------------------------------------------------------------
    # 实时模式：后台线程按倍率推进
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动实时模式线程（重复调用安全）。"""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._realtime_loop,
                                        name="SimClockThread", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止实时线程（等待当前 tick 结束，最多约一个步长的时间）。"""
        with self._lock:
            self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _realtime_loop(self) -> None:
        """实时线程主循环：每 tick 推进 dt，并按倍率控制墙钟节奏。"""
        while True:
            with self._lock:
                if not self._running:
                    break
                paused = self._paused
                speed = self._speed
            if paused:
                # 暂停期间小睡，避免空转烧 CPU
                _wall_time.sleep(0.05)
                continue
            t0 = _wall_time.perf_counter()
            self._tick()
            # 一个 dt 的仿真时间应占用 dt/speed 的墙钟时间
            budget = self._dt / max(speed, 1e-9) - (_wall_time.perf_counter() - t0)
            if budget > 0:
                _wall_time.sleep(budget)

    def _tick(self) -> None:
        """推进一个 tick：先步进全厂，再累加仿真时间（保证回调内 now() 为上一时刻末）。"""
        cb = self._step_cb
        if cb is not None:
            try:
                cb(self._dt)
            except Exception:
                # 单个 tick 异常不允许杀死时钟线程：打印后继续（自检会另行捕获逻辑错误）
                import traceback
                traceback.print_exc()
        self._advance()

    def _advance(self, dt: Optional[float] = None) -> None:
        """时间累加统一入口：逐次 9 位舍入，消除 0.1 浮点漂移（长跑批确定性关键）。"""
        step = self._dt if dt is None else dt
        with self._lock:
            self._sim_now = round(self._sim_now + step, 9)
            self._tick_count += 1

    # ------------------------------------------------------------------
    # 加速/批量模式：同步满速推进（自检冒烟与跑批用）
    # ------------------------------------------------------------------
    def run_until(self, sim_end: float, step_fn: Optional[Callable[[float], None]] = None) -> None:
        """
        同步推进仿真时间直到 sim_end（不含）。
        - step_fn 未给则使用注册的 step 回调（与实时模式同一路径 → 结果一致）；
        - 要求处于非暂停状态（批量模式由调用方完全掌控，暂停无意义）；
        - 不做任何墙钟睡眠，速度取决于计算量。
        """
        if self.is_paused():
            raise RuntimeError("run_until 要求时钟未处于暂停状态（请先 resume）")
        cb = step_fn if step_fn is not None else self._step_cb
        while True:
            with self._lock:
                if self._sim_now >= sim_end:
                    break
            if cb is not None:
                try:
                    cb(self._dt)
                except Exception:
                    import traceback
                    traceback.print_exc()
            self._advance()

    def advance_ticks(self, n: int, step_fn: Optional[Callable[[float], None]] = None) -> None:
        """精确推进 n 个 tick（单测/联锁验证用，避免边界误差）。"""
        cb = step_fn if step_fn is not None else self._step_cb
        for _ in range(int(n)):
            if cb is not None:
                cb(self._dt)
            self._advance()


# ----------------------------------------------------------------------
# 自模块快速自检：python core/sim_clock.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    c = SimClock(dt=0.05, speed=60)
    seen = []
    c.set_step_callback(lambda d: seen.append(c.now()))
    # 加速模式数学验证：推进 200 个 tick = 10 仿真秒
    c.run_until(10.0)
    assert abs(c.now() - 10.0) < 1e-6, "run_until 时间精度错误"
    assert len(seen) == 199 or len(seen) == 200, "tick 数异常"
    # 暂停语义验证：暂停后 run_until 应拒绝
    c.pause()
    try:
        c.run_until(20.0)
        raise AssertionError("暂停状态下 run_until 未拒绝")
    except RuntimeError:
        pass
    c.resume()
    # 实时模式验证：60x 下 0.5 秒墙钟 ≈ 30 仿真秒（放宽 ±50% 容差防 CI 抖动）
    mark = c.now()
    c.start()
    _wall_time.sleep(0.5)
    c.pause()
    gained = c.now() - mark
    c.stop()
    assert 15.0 <= gained <= 45.0, f"实时倍率异常: 0.5s 墙钟仅推进 {gained} 仿真秒"
    print(f"[sim_clock 自检通过] 快速推进 now={c.now()}s, ticks={c.tick_count}, "
          f"实时60x增益={gained:.1f}s (仿真验证值)")
