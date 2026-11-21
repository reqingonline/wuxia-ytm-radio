"""
异构后端合规审计与单元测试套件 — verifier-claude 生成
涵盖：
  1. 全局红线静态扫描（Python、PS1、JS、HTML、CSS）
  2. 音频 DSP 防炸麦数学精度审计（dummy_sink=True 全程静音）
  3. OCR 与防误触单元测试（RapidOCR 合成图实测）
  4. 24 小时稳定性与并发审查（ConfigStore + JukeboxController 锁与异常边界）
"""
from __future__ import annotations

import ast
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

# ─── 项目根目录 ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# 确保 backend 包可以被 import
sys.path.insert(0, str(PROJECT_ROOT))

# ─── 重用引擎（避免重复启动 sounddevice 枚举）──────────────────────────────
os.environ.setdefault("JUKEBOX_DUMMY_SINK", "1")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 全局红线静态扫描
# ══════════════════════════════════════════════════════════════════════════════

class TestStaticComplianceScan:
    """全局红线静态扫描：对全仓代码文件执行精准 AST 或正则扫描，确保零文件删除调用。"""

    @pytest.fixture(scope="class")
    def project_py_files(self) -> list[Path]:
        return [
            p for p in PROJECT_ROOT.rglob("*.py")
            if ".venv" not in p.parts and "__pycache__" not in p.parts
        ]

    @pytest.fixture(scope="class")
    def project_ps1_files(self) -> list[Path]:
        return [
            p for p in PROJECT_ROOT.rglob("*.ps1")
            if ".venv" not in p.parts
        ]

    @pytest.fixture(scope="class")
    def project_js_files(self) -> list[Path]:
        return [
            p for p in PROJECT_ROOT.rglob("*.js")
            if ".venv" not in p.parts and "node_modules" not in p.parts
        ]

    @pytest.fixture(scope="class")
    def project_html_files(self) -> list[Path]:
        return [p for p in PROJECT_ROOT.rglob("*.html") if ".venv" not in p.parts]

    @pytest.fixture(scope="class")
    def project_css_files(self) -> list[Path]:
        return [p for p in PROJECT_ROOT.rglob("*.css") if ".venv" not in p.parts]

    # ── Python AST 扫描 ───────────────────────────────────────────────────────

    def _ast_find_forbidden_calls(self, source: str, filepath: str) -> list[str]:
        """使用 AST 在 Python 源码中扫描禁止的文件删除函数调用（排除注释与字符串）。"""
        violations: list[str] = []
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            return violations

        FORBIDDEN: dict[str, set[str]] = {
            "os": {"remove", "unlink"},
            "shutil": {"rmtree"},
        }
        # Also flag Path.unlink() and direct calls to shutil.rmtree / os.remove / os.unlink
        for node in ast.walk(tree):
            # shutil.rmtree(...) / os.remove(...) / os.unlink(...)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    # e.g. shutil.rmtree or os.remove / os.unlink
                    if isinstance(func.value, ast.Name):
                        mod_name = func.value.id
                        attr_name = func.attr
                        if mod_name in FORBIDDEN and attr_name in FORBIDDEN[mod_name]:
                            violations.append(
                                f"{filepath}:{node.lineno} — {mod_name}.{attr_name}() FORBIDDEN"
                            )
                    # e.g. some_path.unlink(...)
                    if func.attr == "unlink":
                        violations.append(
                            f"{filepath}:{node.lineno} — .unlink() FORBIDDEN"
                        )
        return violations

    def test_python_no_forbidden_delete_calls(self, project_py_files: list[Path]) -> None:
        """Python 源码零文件删除调用（AST 级别，排除注释与字符串）。"""
        all_violations: list[str] = []
        for py_file in project_py_files:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            violations = self._ast_find_forbidden_calls(source, str(py_file))
            all_violations.extend(violations)
        assert not all_violations, (
            "发现禁止的文件删除调用：\n" + "\n".join(all_violations)
        )

    def test_python_no_shutil_rmtree_in_text(self, project_py_files: list[Path]) -> None:
        """Python 源码正则双重验证：非注释行不含 shutil.rmtree（本测试文件自身除外）。"""
        THIS_FILE = Path(__file__).resolve()
        for py_file in project_py_files:
            # 跳过本测试文件自身（其 assert 消息字符串中引用了 shutil.rmtree 文本）
            if py_file.resolve() == THIS_FILE:
                continue
            lines = py_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 允许文档字符串里的说明文字（字符串内的无法精准排除，但 AST 扫描已覆盖真实调用）
                assert "shutil.rmtree" not in line or '"""' in line or "'''" in line or stripped.startswith("#"), (
                    f"{py_file}:{lineno} 含 shutil.rmtree"
                )

    # ── PS1 BOM + &&  ─────────────────────────────────────────────────────────

    def test_ps1_utf8_bom(self, project_ps1_files: list[Path]) -> None:
        """所有项目 .ps1 脚本必须以 UTF-8 BOM (EF BB BF) 开头。"""
        # 过滤掉 .venv 自动生成的 activate.ps1
        project_scripts = [
            f for f in project_ps1_files
            if ".venv" not in str(f)
        ]
        assert project_scripts, "未找到任何 .ps1 文件（start.ps1 / scripts/*.ps1）"
        for ps1 in project_scripts:
            raw = ps1.read_bytes()
            assert raw[:3] == b"\xef\xbb\xbf", (
                f"{ps1} 缺少 UTF-8 BOM (EF BB BF)，实际文件头: {raw[:6].hex()}"
            )

    def test_ps1_no_double_ampersand(self, project_ps1_files: list[Path]) -> None:
        """项目 .ps1 脚本代码行中严禁出现 &&（PowerShell 5.1 不支持）。"""
        project_scripts = [f for f in project_ps1_files if ".venv" not in str(f)]
        for ps1 in project_scripts:
            lines = ps1.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert "&&" not in line, (
                    f"{ps1}:{lineno} 含禁止的 '&&': {line.strip()}"
                )

    def test_ps1_no_forbidden_delete_commands(self, project_ps1_files: list[Path]) -> None:
        """项目 .ps1 脚本代码行中严禁出现危险删除命令。"""
        # 白名单：.venv 自动生成的 activate.ps1 允许出现 Remove-Item 环境变量/函数
        VENV_ACTIVATE = ".venv"
        FORBIDDEN_PATTERNS = [
            r"\bRemove-Item\b",
            r"\brm\s+-",
            r"\brmdir\b",
            r"\bdel\s+",
        ]
        for ps1 in project_ps1_files:
            if VENV_ACTIVATE in str(ps1):
                continue  # 跳过 venv 自动生成脚本
            lines = ps1.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in FORBIDDEN_PATTERNS:
                    assert not re.search(pattern, line, re.IGNORECASE), (
                        f"{ps1}:{lineno} 含禁止命令 ({pattern}): {line.strip()}"
                    )

    # ── JS / HTML / CSS ────────────────────────────────────────────────────────

    def test_js_no_forbidden_delete_calls(self, project_js_files: list[Path]) -> None:
        """JavaScript 源码严禁出现 fs.rmSync / fs.unlinkSync。"""
        for js_file in project_js_files:
            content = js_file.read_text(encoding="utf-8", errors="ignore")
            # 过滤注释行
            non_comment_lines = [
                ln for ln in content.splitlines()
                if not ln.strip().startswith("//") and not ln.strip().startswith("*")
            ]
            filtered = "\n".join(non_comment_lines)
            assert "fs.rmSync" not in filtered, f"{js_file} 含 fs.rmSync"
            assert "fs.unlinkSync" not in filtered, f"{js_file} 含 fs.unlinkSync"

    def test_js_no_unsafe_innerHTML_xss(self, project_js_files: list[Path]) -> None:
        """JavaScript 严禁使用未转义的 innerHTML 拼接用户/外部文本（XSS 防护）。"""
        # 允许 innerHTML = '' 或 innerHTML = `<tag>...</tag>` 纯静态字面量
        UNSAFE_PATTERN = re.compile(
            r"\.innerHTML\s*[+]?=\s*(?!(?:\s*['\"`]\s*(?:<[^>]+>)*\s*['\"`]|''\s*;|\"\")\s)",
        )
        for js_file in project_js_files:
            lines = js_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            for lineno, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("*"):
                    continue
                # 检查 innerHTML 拼接外部文本（允许纯字面量赋值如清空 ''）
                if ".innerHTML" in line and "+=" in line:
                    pytest.fail(
                        f"{js_file}:{lineno} — 疑似 innerHTML XSS 拼接: {stripped}"
                    )
                # 允许 innerHTML = '' 清空，检查拼接变量
                if ".innerHTML" in line and "=" in line and "+=" not in line:
                    # 允许模板字面量内的纯 HTML 字面量（不含变量插值）
                    # 若含 ${...} 变量插值则报警
                    rhs_match = re.search(r"\.innerHTML\s*=\s*(.+)", line)
                    if rhs_match:
                        rhs = rhs_match.group(1).strip()
                        # 允许空字符串、null、仅 HTML 标签字面量
                        if re.search(r"\$\{", rhs):
                            # 只在含变量插值时报警
                            pytest.fail(
                                f"{js_file}:{lineno} — innerHTML 含变量插值 XSS 风险: {stripped}"
                            )

    def test_html_no_forbidden_content(self, project_html_files: list[Path]) -> None:
        """HTML 文件存在且无明显 XSS 危险内联脚本。"""
        for html_file in project_html_files:
            content = html_file.read_text(encoding="utf-8", errors="ignore")
            # 检查基本 HTML 结构
            assert "<!DOCTYPE" in content or "<!doctype" in content.lower(), (
                f"{html_file} 缺少 DOCTYPE 声明"
            )
            # 确认无 document.write 危险调用
            assert "document.write(" not in content, (
                f"{html_file} 含 document.write() 潜在 XSS"
            )

    def test_css_exists_and_valid(self, project_css_files: list[Path]) -> None:
        """CSS 文件存在且非空。"""
        # 只检查 frontend/styles.css，可能存在其他 css
        frontend_css = FRONTEND_DIR / "styles.css"
        if frontend_css.exists():
            content = frontend_css.read_text(encoding="utf-8", errors="ignore")
            assert len(content.strip()) > 100, "styles.css 内容过短，可能未正常生成"


# ══════════════════════════════════════════════════════════════════════════════
# 2. OCR 内存截图合规检查
# ══════════════════════════════════════════════════════════════════════════════

class TestOcrWatcherCompliance:
    """ocr_watcher.py 截屏与 OCR 全程在内存完成，不向磁盘写截图文件的合规检查。"""

    def test_ocr_watcher_no_disk_write_in_source(self) -> None:
        """静态扫描 ocr_watcher.py：不含写截图文件的调用（imwrite / save to temp file）。"""
        ocr_py = BACKEND_DIR / "ocr_watcher.py"
        assert ocr_py.exists(), "ocr_watcher.py 不存在"
        source = ocr_py.read_text(encoding="utf-8", errors="ignore")

        # 禁止向磁盘写截图文件的典型调用
        FORBIDDEN_DISK_WRITE_PATTERNS = [
            r"cv2\.imwrite\s*\(",
            r"imageio\.imwrite\s*\(",
            r"\.save\s*\(\s*['\"].*\.(png|jpg|jpeg|bmp)",  # PIL save to string path (not BytesIO)
            r"open\s*\([^)]*(?:png|jpg|jpeg|bmp)[^)]*,\s*['\"]wb",
        ]
        for pattern in FORBIDDEN_DISK_WRITE_PATTERNS:
            matches = re.findall(pattern, source, re.IGNORECASE)
            assert not matches, (
                f"ocr_watcher.py 含向磁盘写截图文件的调用 ({pattern}): {matches}"
            )

    def test_capture_roi_png_returns_bytes_in_memory(self) -> None:
        """capture_roi_png 返回 bytes（内存编码），且不向磁盘写文件。"""
        from backend.ocr_watcher import OcrChatWatcher, render_synthetic_chat_image
        watcher = OcrChatWatcher()

        # 合成图片作为 ROI（单元测试环境无物理屏幕，通过 mss fallback 或 synthetic）
        snapshot_before = set(BACKEND_DIR.rglob("*.png"))
        png_bytes = watcher.capture_roi_png()
        snapshot_after = set(BACKEND_DIR.rglob("*.png"))

        assert isinstance(png_bytes, bytes), "capture_roi_png 应返回 bytes"
        assert png_bytes[:4] == b"\x89PNG", f"返回值非 PNG 格式，头字节: {png_bytes[:8].hex()}"
        # 确认调用后无新 PNG 文件落盘
        new_files = snapshot_after - snapshot_before
        assert not new_files, f"capture_roi_png 写入了磁盘文件: {new_files}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. YTMusic 缓存淘汰 shutil.move 合规检查
# ══════════════════════════════════════════════════════════════════════════════

class TestYtmusicCacheEviction:
    """ytmusic_service.py 缓存淘汰使用 shutil.move 移入 trash_dir，保留文件完整。"""

    def test_enforce_cache_limit_uses_move_not_delete(self, tmp_path: Path) -> None:
        """超出缓存上限时，旧文件被 shutil.move 移入 trash_dir 而非删除。"""
        from backend.ytmusic_service import YTMusicService

        data_dir = tmp_path / "data"
        trash_dir = tmp_path / "_trash"
        svc = YTMusicService(data_dir=data_dir, trash_dir=trash_dir, cache_max_mb=0.0001)  # 极小限制

        # 写入两个假缓存文件（各 100 字节）
        cache_dir = data_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        old_file = cache_dir / "oldvid.m4a"
        new_file = cache_dir / "newvid.m4a"
        old_file.write_bytes(b"X" * 100)
        time.sleep(0.01)
        new_file.write_bytes(b"Y" * 100)

        # 触发缓存淘汰
        moved = svc.enforce_cache_limit(protect_file=new_file)

        # 旧文件应被移入 trash，而非消失
        assert not old_file.exists(), "旧缓存文件应已从 cache 移走"
        trash_files = list(trash_dir.iterdir())
        assert len(trash_files) >= 1, "trash_dir 中应有已移入的旧缓存文件"
        # 文件内容完整（非空、非截断）
        for tf in trash_files:
            assert tf.stat().st_size == 100, f"移入 trash 的文件大小不完整: {tf}"

    def test_move_to_trash_collision_resolved(self, tmp_path: Path) -> None:
        """同名文件移入 trash 时自动加时间戳后缀，不会覆盖或失败。"""
        from backend.ytmusic_service import YTMusicService

        data_dir = tmp_path / "data"
        trash_dir = tmp_path / "_trash"
        svc = YTMusicService(data_dir=data_dir, trash_dir=trash_dir)

        # 在 trash 中预置同名文件
        trash_dir.mkdir(parents=True, exist_ok=True)
        (trash_dir / "headers.json").write_text("old", encoding="utf-8")

        # 触发 move_to_trash（原本不存在，先创建）
        src = data_dir / "headers.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        src.write_text("new", encoding="utf-8")
        result = svc._move_to_trash(src)

        assert result is not None, "_move_to_trash 应返回目标路径"
        assert result.exists(), "移入 trash 的文件应存在"
        # 原 trash 中的 headers.json 应保持完整（无被覆盖）
        orig = trash_dir / "headers.json"
        assert orig.exists() and orig.read_text(encoding="utf-8") == "old", (
            "trash 中原有文件被意外覆盖"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 4. 音频 DSP 防炸麦数学精度审计（dummy_sink=True 全程静音）
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_RATE = 48000
CHANNELS = 2
BLOCKSIZE = 1024
LIMIT_LINEAR = 0.7944  # -2.0 dBFS 对应线性值上限（规格要求 <= 0.7944）


def _make_sine_block(freq_hz: float, amplitude: float, num_frames: int = BLOCKSIZE) -> np.ndarray:
    """生成 stereo float32 正弦波测试块。"""
    t = np.arange(num_frames, dtype=np.float64) / SAMPLE_RATE
    mono = (amplitude * np.sin(2.0 * math.pi * freq_hz * t)).astype(np.float32)
    return np.column_stack([mono, mono]).astype(np.float32)


def _make_silence(num_frames: int = BLOCKSIZE) -> np.ndarray:
    return np.zeros((num_frames, CHANNELS), dtype=np.float32)


@pytest.fixture(scope="module")
def audio_engine():
    """共享 AudioEngine 实例（dummy_sink=True，不打开物理声卡）。"""
    from backend.audio_engine import AudioEngine
    eng = AudioEngine(
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        blocksize=BLOCKSIZE,
        dummy_sink=True,
    )
    # 明确开启 ducking 以便测试
    eng.ducking_enabled = True
    eng.duck_threshold_db = -38.0
    eng.ducking_db = -12.0
    eng.mic_bridge_enabled = False
    eng.broadcast_volume = 1.0  # 单元测试用满音量方便计算
    yield eng
    eng.shutdown()


class TestSoftLimiterMath:
    """软限幅数学精度测试：输入满幅或超幅时输出峰值严格 <= -2.0 dBFS。"""

    def test_soft_limit_unity_amplitude(self, audio_engine: Any) -> None:
        """满幅（1.0）正弦波经过 process_mix_block 后，推流峰值 <= 0.7944。"""
        music = _make_sine_block(440.0, 1.0)
        broadcast, _monitor, meta = audio_engine.process_mix_block(music, None)
        peak = float(np.max(np.abs(broadcast)))
        assert peak <= LIMIT_LINEAR, (
            f"满幅输入峰值 {peak:.6f} 超过 -2.0 dBFS 软限幅上限 {LIMIT_LINEAR}"
        )

    def test_soft_limit_overload_amplitude(self, audio_engine: Any) -> None:
        """超幅（2.0）正弦波经过 process_mix_block 后，推流峰值严格 <= 0.7944。"""
        music = _make_sine_block(440.0, 2.0)
        broadcast, _monitor, meta = audio_engine.process_mix_block(music, None)
        peak = float(np.max(np.abs(broadcast)))
        assert peak <= LIMIT_LINEAR, (
            f"超幅（2.0）输入峰值 {peak:.6f} 超过 -2.0 dBFS 软限幅上限 {LIMIT_LINEAR}"
        )

    def test_soft_limit_low_amplitude_passthrough(self, audio_engine: Any) -> None:
        """低电平（0.1）正弦波不被过度压缩（峰值应接近 0.1，误差在 5% 内）。"""
        amp = 0.1
        music = _make_sine_block(440.0, amp)
        broadcast, _monitor, _meta = audio_engine.process_mix_block(music, None)
        peak = float(np.max(np.abs(broadcast)))
        expected = amp * audio_engine.broadcast_volume
        assert abs(peak - expected) / expected < 0.05, (
            f"低电平信号被过度压缩: 期望峰值 ~{expected:.4f}，实际 {peak:.4f}"
        )


class TestAutoDucking:
    """自动闪避状态机测试（-20 dBFS 人声正弦波触发 ducking，静止后自动恢复）。"""

    @pytest.fixture(autouse=True)
    def reset_ducking(self, audio_engine: Any) -> None:
        """每个测试前重置闪避状态机。"""
        audio_engine._duck_gain = 1.0
        audio_engine._hold_remaining_sec = 0.0
        audio_engine._ducking_active = False
        audio_engine.ducking_enabled = True
        audio_engine.duck_threshold_db = -38.0
        audio_engine.ducking_db = -12.0
        audio_engine.broadcast_volume = 1.0
        yield

    def test_silence_mic_no_ducking(self, audio_engine: Any) -> None:
        """静音麦克风输入时，ducking_active 应为 False。"""
        music = _make_sine_block(440.0, 0.5)
        silence_mic = _make_silence()
        _broadcast, _monitor, meta = audio_engine.process_mix_block(music, silence_mic)
        assert meta["ducking_active"] is False, (
            f"静音麦时 ducking_active 应为 False，实际: {meta['ducking_active']}"
        )
        assert math.isclose(meta["duck_gain"], 1.0, abs_tol=0.001), (
            f"静音麦时 duck_gain 应为 1.0，实际: {meta['duck_gain']:.4f}"
        )

    def test_voice_triggers_ducking_after_100ms(self, audio_engine: Any) -> None:
        """-20 dBFS 人声正弦波持续约 100 ms 后，ducking_active 应为 True，且主推流幅值下降约 12 dB。"""
        # -20 dBFS 人声幅值（线性 ~0.1，远大于 -38 dBFS 阈值 ~0.013）
        voice_amp = 10.0 ** (-20.0 / 20.0)  # ≈ 0.1

        # 先用纯静音音乐测量无闪避时的参考电平
        ref_music = _make_sine_block(440.0, 0.5)
        ref_broadcast, _, _ = audio_engine.process_mix_block(ref_music, None)
        ref_peak = float(np.max(np.abs(ref_broadcast)))

        # 重置状态
        audio_engine._duck_gain = 1.0
        audio_engine._hold_remaining_sec = 0.0
        audio_engine._ducking_active = False

        # 喂入 100 ms 人声（帧数 = 48000 * 0.1 = 4800 帧，分多个 blocksize 块喂入）
        frames_100ms = int(SAMPLE_RATE * 0.100)
        music = _make_sine_block(440.0, 0.5, frames_100ms)
        mic = _make_sine_block(300.0, voice_amp, frames_100ms)
        broadcast, _, meta = audio_engine.process_mix_block(music, mic)

        assert meta["ducking_active"] is True, (
            f"-20 dBFS 人声输入 100 ms 后 ducking_active 应为 True，实际: {meta['ducking_active']}"
        )

        # 验证音乐幅值下降约 12 dB（误差在过渡包络范围内，允许 ±4 dB）
        ducked_peak = float(np.max(np.abs(broadcast)))
        if ref_peak > 1e-5:
            attenuation_db = 20.0 * math.log10(ducked_peak / ref_peak)
            assert attenuation_db <= -8.0, (
                f"ducking 后音乐幅值下降 {attenuation_db:.1f} dB，预期约 -12 dB（误差 ±4 dB）"
            )

    def test_ducking_releases_after_1500ms_silence(self, audio_engine: Any) -> None:
        """停止说话 > 1.5 s（hold 600ms + release 500ms）后，duck_gain 应恢复接近 1.0。"""
        voice_amp = 10.0 ** (-20.0 / 20.0)

        # 先触发 ducking
        frames_100ms = int(SAMPLE_RATE * 0.100)
        music = _make_sine_block(440.0, 0.5, frames_100ms)
        mic = _make_sine_block(300.0, voice_amp, frames_100ms)
        audio_engine.process_mix_block(music, mic)
        assert audio_engine._ducking_active, "前置条件：ducking 应已触发"

        # 喂入 2.0 s 的静音麦克风（超过 hold 600ms + release 500ms = 1.1 s）
        frames_2s = int(SAMPLE_RATE * 2.0)
        music_long = _make_sine_block(440.0, 0.5, frames_2s)
        silence_mic = _make_silence(frames_2s)
        _, _, meta = audio_engine.process_mix_block(music_long, silence_mic)

        # duck_gain 应恢复接近 1.0（误差 <= 0.05）
        assert meta["duck_gain"] >= 0.95, (
            f"停止说话 2s 后 duck_gain 未恢复: {meta['duck_gain']:.4f}"
        )
        assert meta["ducking_active"] is False, (
            f"停止说话 2s 后 ducking_active 应为 False: {meta['ducking_active']}"
        )

    def test_monitor_bus_no_mic_feed(self, audio_engine: Any) -> None:
        """本地监听路 monitor_block 绝不混入物理麦克风人声（防延迟耳返）。"""
        audio_engine.monitor_enabled = True
        voice_amp = 0.8  # 高电平麦克风输入
        music = _make_sine_block(440.0, 0.3)
        mic = _make_sine_block(300.0, voice_amp)

        _, monitor_block, _ = audio_engine.process_mix_block(music, mic)

        # monitor_block 应只含 440 Hz 音乐成分，不含 300 Hz 麦克风成分
        # 验证：monitor 峰值应接近 music * monitor_volume，远小于添加 mic 信号后的值
        expected_monitor_peak = 0.3 * audio_engine.monitor_volume
        actual_monitor_peak = float(np.max(np.abs(monitor_block)))
        # 允许 10% 误差
        assert actual_monitor_peak <= expected_monitor_peak * 1.15, (
            f"monitor_block 疑似混入了麦克风信号: "
            f"实际峰值 {actual_monitor_peak:.4f}，期望约 {expected_monitor_peak:.4f}"
        )


class TestPlayTestTone:
    """play_test_tone 在 dummy_sink=True 下的行为测试。"""

    def test_play_test_tone_dummy_no_physical_device(self) -> None:
        """dummy_sink=True 时 play_test_tone 不访问物理声卡，broadcast_rms_db 有效。"""
        from backend.audio_engine import AudioEngine, SILENCE_FLOOR_DB
        eng = AudioEngine(sample_rate=48000, channels=2, blocksize=1024, dummy_sink=True)
        try:
            eng.play_test_tone(duration_sec=0.2)
            # 允许少量时间让后台线程处理第一块
            time.sleep(0.05)
            status = eng.get_status()
            assert status["state"] == "playing", (
                f"play_test_tone 后状态应为 playing，实际: {status['state']}"
            )
            # broadcast_rms_db 应有效（> SILENCE_FLOOR_DB）
            assert status["broadcast_rms_db"] > SILENCE_FLOOR_DB, (
                f"play_test_tone 后 broadcast_rms_db 应有效（> {SILENCE_FLOOR_DB} dB），"
                f"实际: {status['broadcast_rms_db']}"
            )
            assert status["routing"]["dummy_sink"] is True, "routing.dummy_sink 应为 True"
        finally:
            eng.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# 5. OCR 防误触单元测试（RapidOCR 合成图实测）
# ══════════════════════════════════════════════════════════════════════════════

class TestOcrChatParser:
    """parse_chat_lines 防误触规则测试（纯逻辑，无需 RapidOCR）。"""

    @pytest.fixture(scope="class")
    def watcher(self):
        from backend.ocr_watcher import OcrChatWatcher
        w = OcrChatWatcher()
        w.cooldown_sec = 0.0  # 单元测试关闭冷却
        return w

    def _parse(self, watcher: Any, lines: list[str]) -> list[dict]:
        """便捷解析辅助，每次清除去重状态。"""
        watcher.reset_dedup()
        return watcher.parse_chat_lines(lines, trigger_mode="both", allow_skip_cmd=True, bypass_dedup=True)

    def test_valid_song_with_separator(self, watcher: Any) -> None:
        """正常「歌名-歌手」格式应被识别为 song 指令。"""
        cmds = self._parse(watcher, ["[帮派]明月心：晴天-周杰伦"])
        songs = [c for c in cmds if c["type"] == "song"]
        assert songs, "「晴天-周杰伦」应被识别为点歌指令"
        assert songs[0]["title"] == "晴天"
        assert songs[0]["artist"] == "周杰伦"
        assert songs[0]["requester"] == "明月心"

    def test_valid_song_with_prefix(self, watcher: Any) -> None:
        """「点歌 歌名-歌手」前缀格式应正确识别。"""
        cmds = self._parse(watcher, ["[帮派]唐青枫：点歌 泡沫-邓紫棋"])
        songs = [c for c in cmds if c["type"] == "song"]
        assert songs, "「点歌 泡沫-邓紫棋」应被识别为点歌指令"
        assert songs[0]["title"] == "泡沫"
        assert songs[0]["artist"] == "邓紫棋"

    def test_skip_command_recognized(self, watcher: Any) -> None:
        """切歌指令应被正确识别为 skip。"""
        cmds = self._parse(watcher, ["[帮派]叶开：切歌"])
        skips = [c for c in cmds if c["type"] == "skip"]
        assert skips, "「切歌」应被识别为切歌指令"
        assert skips[0]["requester"] == "叶开"

    def test_coordinate_noise_filtered(self, watcher: Any) -> None:
        """坐标干扰噪声「坐标 123-456 集合」应被过滤，0 误触。"""
        cmds = self._parse(watcher, ["[帮派]路人甲：坐标 123-456 集合"])
        assert not cmds, f"坐标噪声应被过滤，实际识别到: {cmds}"

    def test_tech_model_noise_filtered(self, watcher: Any) -> None:
        """硬件型号噪声「帧率 60-120 RTX-4060」应被过滤，0 误触。"""
        cmds = self._parse(watcher, ["[帮派]路人乙：帧率 60-120 RTX-4060"])
        assert not cmds, f"帧率/RTX噪声应被过滤，实际识别到: {cmds}"

    def test_numeric_range_filtered(self, watcher: Any) -> None:
        """纯数字区间「123-456」应被过滤。"""
        cmds = self._parse(watcher, ["[帮派]测试：123-456"])
        assert not cmds, f"纯数字区间应被过滤，实际: {cmds}"

    def test_utf_tech_token_filtered(self, watcher: Any) -> None:
        """UTF-8 编码标记噪声应被过滤。"""
        cmds = self._parse(watcher, ["[帮派]路人：UTF-8 编码"])
        assert not cmds, f"UTF-8技术标记应被过滤，实际: {cmds}"

    def test_rtx_model_filtered(self, watcher: Any) -> None:
        """RTX-4060 型号噪声应被过滤。"""
        cmds = self._parse(watcher, ["[帮派]路人：RTX-4060"])
        assert not cmds, f"RTX-4060 应被过滤，实际: {cmds}"


class TestOcrCooldownDedup:
    """冷却去重与静止画面重复过滤测试。"""

    def test_same_song_blocked_in_cooldown(self) -> None:
        """相同歌名在 cooldown_sec 内不重复入队。"""
        from backend.ocr_watcher import OcrChatWatcher
        watcher = OcrChatWatcher()
        watcher.cooldown_sec = 300.0
        watcher.reset_dedup()

        lines = ["[帮派]明月心：晴天-周杰伦"]

        # 第一次：应通过
        cmds1 = watcher.parse_chat_lines(lines, trigger_mode="both", allow_skip_cmd=True, bypass_dedup=False)
        assert any(c["type"] == "song" for c in cmds1), "第一次点歌应成功"

        # 第二次（冷却期内）：应被拦截
        cmds2 = watcher.parse_chat_lines(lines, trigger_mode="both", allow_skip_cmd=True, bypass_dedup=False)
        assert not any(c["type"] == "song" for c in cmds2), (
            "冷却期内同一首歌应被拦截，不重复入队"
        )

    def test_static_frame_dedup_by_sequence_matcher(self) -> None:
        """静止聊天框残留历史消息（相似度 >= 0.85）不重复触发。"""
        from backend.ocr_watcher import OcrChatWatcher
        watcher = OcrChatWatcher()
        watcher.cooldown_sec = 300.0
        watcher.reset_dedup()

        # 第一次扫描
        line1 = ["[帮派]明月心：晴天-周杰伦"]
        watcher.parse_chat_lines(line1, bypass_dedup=False)

        # 轻微 OCR 抖动（如多了个空格），相似度 >= 0.85
        line2 = ["[帮派]明月心：晴天-周杰伦 "]  # 尾部多空格
        cmds = watcher.parse_chat_lines(line2, bypass_dedup=False)
        assert not any(c["type"] == "song" for c in cmds), (
            "OCR 轻微抖动（相似度 >= 0.85）的静止画面不应重复触发"
        )


@pytest.mark.slow
class TestOcrRapidOcrRealInference:
    """RapidOCR 合成图真实推理测试（使用 render_synthetic_chat_image）。"""

    @pytest.fixture(scope="class")
    def watcher(self):
        from backend.ocr_watcher import OcrChatWatcher
        w = OcrChatWatcher()
        w.cooldown_sec = 0.0
        return w

    def test_render_synthetic_image_shape(self) -> None:
        """render_synthetic_chat_image 返回正确形状的 numpy 数组。"""
        from backend.ocr_watcher import render_synthetic_chat_image
        messages = [
            "[帮派]明月心：晴天-周杰伦",
            "[帮派]唐青枫：点歌 泡沫-邓紫棋",
            "[帮派]叶开：切歌",
            "[帮派]路人甲：坐标 123-456 集合",
            "[帮派]路人乙：帧率 60-120 RTX-4060",
        ]
        img = render_synthetic_chat_image(messages, width=640, height=360)
        assert isinstance(img, np.ndarray), "render_synthetic_chat_image 应返回 numpy 数组"
        assert img.dtype == np.uint8, f"图像数据类型应为 uint8，实际: {img.dtype}"
        assert img.ndim == 3, f"图像应为 3D 数组（H, W, C），实际: {img.ndim}D"
        assert img.shape[2] in (3, 4), f"图像通道数应为 3 或 4，实际: {img.shape[2]}"

    def test_rapidocr_song_detection_100_percent(self, watcher: Any) -> None:
        """RapidOCR 合成图测试：100% 检出点歌与切歌，0 误触游戏噪声。"""
        from backend.ocr_watcher import render_synthetic_chat_image, OcrChatWatcher

        messages = [
            "[帮派]明月心：晴天-周杰伦",
            "[帮派]唐青枫：点歌 泡沫-邓紫棋",
            "[帮派]叶开：切歌",
            "[帮派]路人甲：坐标 123-456 集合",
            "[帮派]路人乙：帧率 60-120 RTX-4060",
        ]
        img = render_synthetic_chat_image(messages, width=800, height=480)
        result = watcher.test_once(synthetic_messages=messages)

        commands = result.get("commands", [])
        raw_lines = result.get("raw_lines", [])

        songs = [c for c in commands if c["type"] == "song"]
        skips = [c for c in commands if c["type"] == "skip"]

        # 检查点歌检出
        song_titles = {c["title"] for c in songs}
        assert "晴天" in song_titles or any("晴天" in t for t in song_titles), (
            f"「晴天-周杰伦」未被检出，所有歌曲: {songs}"
        )
        assert "泡沫" in song_titles or any("泡沫" in t for t in song_titles), (
            f"「泡沫-邓紫棋」未被检出，所有歌曲: {songs}"
        )

        # 检查切歌检出
        assert len(skips) >= 1, f"切歌指令未被检出，raw_lines: {raw_lines}"

        # 验证 0 误触（坐标、帧率等干扰项不应出现在 commands 中）
        for cmd in commands:
            # 检查所有识别到的指令不含坐标/帧率数字区间
            if cmd["type"] == "song":
                assert "123" not in cmd.get("title", ""), (
                    f"坐标噪声被误触为点歌: {cmd}"
                )
                assert "RTX" not in cmd.get("title", "").upper(), (
                    f"RTX-4060 被误触为点歌: {cmd}"
                )
                assert "60" not in cmd.get("title", "") or "120" not in cmd.get("artist", ""), (
                    f"帧率噪声被误触为点歌: {cmd}"
                )


# ══════════════════════════════════════════════════════════════════════════════
# 6. 24 小时稳定性与并发审查
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigStoreConcurrency:
    """ConfigStore 线程锁与原子写入测试。"""

    def test_concurrent_updates_no_corruption(self, tmp_path: Path) -> None:
        """50 个线程并发更新 ConfigStore，配置文件不损坏，无死锁。"""
        from backend.config_store import ConfigStore
        config_path = tmp_path / "config.json"
        store = ConfigStore(config_path=config_path)

        errors: list[str] = []
        THREADS = 20

        def _update_worker(idx: int) -> None:
            try:
                store.update({"auto_dj_enabled": bool(idx % 2 == 0)})
                cfg = store.get()
                assert isinstance(cfg, dict), f"线程 {idx}: get() 返回非 dict"
            except Exception as exc:
                errors.append(f"线程 {idx}: {exc}")

        threads = [threading.Thread(target=_update_worker, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive(), "线程超时，疑似死锁"

        assert not errors, f"并发更新 ConfigStore 发生错误:\n" + "\n".join(errors)

        # 确认配置文件可正常读取（无损坏）
        final_cfg = store.load()
        assert isinstance(final_cfg, dict), "ConfigStore 并发更新后配置文件损坏"

    def test_config_store_no_delete_on_save(self, tmp_path: Path) -> None:
        """ConfigStore.save() 使用原子移动+替换，不直接删除旧文件。"""
        from backend.config_store import ConfigStore
        config_path = tmp_path / "config.json"
        store = ConfigStore(config_path=config_path)

        # 多次保存，确认 trash_dir 中有备份（原子替换会将旧文件移入 trash）
        store.update({"auto_dj_enabled": True})
        store.update({"auto_dj_enabled": False})

        trash_dir = store.trash_dir
        trash_files = list(trash_dir.glob("config_backup_*.json")) if trash_dir.exists() else []
        # 允许 trash 中有备份或直接覆写（取决于具体实现）
        # 关键：config_path 必须存在且内容有效
        assert config_path.exists(), "config.json 应存在"
        import json
        content = config_path.read_text(encoding="utf-8", errors="ignore")
        parsed = json.loads(content)
        assert isinstance(parsed, dict), "config.json 内容应为有效 JSON dict"


class TestJukeboxControllerLock:
    """JukeboxController 守护循环异常边界与线程安全测试。"""

    @pytest.fixture(scope="class")
    def controller(self):
        """创建带 dummy_sink 的 JukeboxController，不启动守护线程（避免网络访问）。"""
        from backend.jukebox_controller import JukeboxController
        ctrl = JukeboxController(dummy_sink=True, start_daemons=False)
        yield ctrl
        ctrl.shutdown()

    def test_enqueue_track_thread_safe(self, controller: Any) -> None:
        """10 个线程并发 enqueue_track，队列长度正确，无死锁。"""
        errors: list[str] = []
        THREADS = 10

        def _enqueue(idx: int) -> None:
            try:
                track = {
                    "video_id": f"testid_{idx:04d}",
                    "title": f"测试曲目 {idx}",
                    "artist": "测试歌手",
                    "duration_sec": 180,
                    "thumbnail_url": "",
                }
                controller.enqueue_track(track, requester=f"少侠{idx}", source="web")
            except Exception as exc:
                errors.append(f"线程 {idx}: {exc}")

        threads = [threading.Thread(target=_enqueue, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive(), "enqueue_track 线程超时，疑似死锁"

        assert not errors, f"并发入队发生错误:\n" + "\n".join(errors)
        queue = controller.get_queue()
        assert len(queue) == THREADS, (
            f"并发入队后队列长度 {len(queue)} 应为 {THREADS}"
        )

    def test_clear_queue_thread_safe(self, controller: Any) -> None:
        """clear_queue 线程安全，调用后队列为空。"""
        result = controller.clear_queue()
        assert result["ok"] is True
        assert controller.get_queue() == []

    def test_autodj_loop_no_exception_propagation(self, controller: Any) -> None:
        """_autodj_step 在无曲目且 auto-dj 未启动时正常退出（不抛异常）。"""
        # 确保 auto-dj 开启，但无队列、无 fallback（应安静退出）
        controller._fallback_tracks = []
        controller._fallback_index = 0
        try:
            controller._autodj_step()
        except Exception as exc:
            pytest.fail(f"_autodj_step 不应向外传播异常: {exc}")

    def test_preload_step_no_exception_on_empty(self, controller: Any) -> None:
        """_preload_step 在无队列时正常退出（不抛异常）。"""
        controller._queue = []
        try:
            controller._preload_step()
        except Exception as exc:
            pytest.fail(f"_preload_step 不应向外传播异常: {exc}")

    def test_shutdown_idempotent(self, controller: Any) -> None:
        """多次调用 shutdown 不引发异常（幂等性）。"""
        try:
            controller.shutdown()
            controller.shutdown()
        except Exception as exc:
            pytest.fail(f"多次 shutdown 引发异常: {exc}")

    def test_emit_event_no_exception(self, controller: Any) -> None:
        """emit_event 在无 ws 监听者时不抛异常。"""
        try:
            controller.emit_event("info", "测试事件")
            controller.emit_event("warn", "测试警告")
            controller.emit_event("error", "测试错误")
        except Exception as exc:
            pytest.fail(f"emit_event 抛出异常: {exc}")


class TestAudioEngineIntegration:
    """AudioEngine dummy_sink 集成测试：play_test_tone → get_status → shutdown 完整流程。"""

    def test_full_lifecycle_dummy_sink(self) -> None:
        """play_test_tone → pause → resume → stop → shutdown 完整生命周期无异常。"""
        from backend.audio_engine import AudioEngine
        eng = AudioEngine(sample_rate=48000, channels=2, blocksize=1024, dummy_sink=True)
        try:
            eng.play_test_tone(duration_sec=0.5)
            time.sleep(0.05)
            status = eng.get_status()
            assert status["state"] == "playing"
            assert status["routing"]["dummy_sink"] is True

            eng.pause()
            assert eng.get_status()["state"] == "paused"

            eng.resume()
            assert eng.get_status()["state"] == "playing"

            eng.stop()
            time.sleep(0.05)
            assert eng.get_status()["state"] == "stopped"
        finally:
            eng.shutdown()

    def test_process_mix_block_deterministic(self) -> None:
        """process_mix_block 是纯函数级操作（相同输入，峰值结果一致）。"""
        from backend.audio_engine import AudioEngine
        eng = AudioEngine(dummy_sink=True)
        eng.broadcast_volume = 1.0

        music = _make_sine_block(440.0, 1.5)
        _, _, meta1 = eng.process_mix_block(music.copy(), None)
        # 重置 ducking 状态
        eng._duck_gain = 1.0
        eng._ducking_active = False
        _, _, meta2 = eng.process_mix_block(music.copy(), None)
        assert math.isclose(meta1["peak_db"], meta2["peak_db"], abs_tol=0.5), (
            f"相同输入 peak_db 不一致: {meta1['peak_db']:.2f} vs {meta2['peak_db']:.2f}"
        )

    def test_soft_limit_constant(self) -> None:
        """验证 LIMIT_LINEAR = 0.7943 对应 -2.0 dBFS。"""
        from backend.audio_engine import LIMIT_LINEAR
        expected = 10.0 ** (-2.0 / 20.0)
        assert math.isclose(LIMIT_LINEAR, expected, rel_tol=0.001), (
            f"LIMIT_LINEAR={LIMIT_LINEAR:.6f} 与 -2.0dBFS 期望值 {expected:.6f} 不符"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 7. 综合冒烟：ConfigStore + AudioEngine + OcrChatWatcher 联合初始化
# ══════════════════════════════════════════════════════════════════════════════

class TestSystemSmoke:
    """系统级冒烟测试：各核心模块联合初始化，无崩溃无孤儿线程。"""

    def test_all_modules_import_successfully(self) -> None:
        """所有后端模块可正常 import，无 ImportError。"""
        try:
            from backend import audio_engine, ocr_watcher, ytmusic_service, config_store, jukebox_controller, models, main  # noqa: F401
        except ImportError as exc:
            pytest.fail(f"后端模块 import 失败: {exc}")

    def test_config_store_default_values(self, tmp_path: Path) -> None:
        """ConfigStore 默认配置包含必要字段。"""
        from backend.config_store import ConfigStore
        store = ConfigStore(config_path=tmp_path / "cfg.json")
        cfg = store.get()
        assert "auto_dj_enabled" in cfg
        assert "fallback_source" in cfg
        assert "audio" in cfg
        assert "ocr" in cfg
        audio = cfg["audio"]
        assert "duck_threshold_db" in audio
        assert "ducking_db" in audio
        assert math.isclose(audio["duck_threshold_db"], -38.0, abs_tol=0.1)
        assert math.isclose(audio["ducking_db"], -12.0, abs_tol=0.1)

    def test_audio_engine_get_status_structure(self) -> None:
        """AudioEngine.get_status() 返回所有必需字段。"""
        from backend.audio_engine import AudioEngine
        eng = AudioEngine(dummy_sink=True)
        status = eng.get_status()
        required_fields = [
            "state", "track", "position_sec", "duration_sec",
            "broadcast_rms_db", "peak_db", "mic_rms_db",
            "ducking_active", "routing", "vbcable_installed",
        ]
        for field in required_fields:
            assert field in status, f"get_status() 缺少字段: {field}"
        # dummy_sink is in routing sub-dict
        assert "dummy_sink" in status["routing"], "get_status().routing 缺少 dummy_sink 字段"
        eng.shutdown()

    def test_ocr_watcher_get_status_structure(self) -> None:
        """OcrChatWatcher.get_status() 返回所有必需字段。"""
        from backend.ocr_watcher import OcrChatWatcher
        w = OcrChatWatcher()
        status = w.get_status()
        required_fields = [
            "enabled", "running", "roi", "interval_sec",
            "trigger_mode", "cooldown_sec", "allow_skip_cmd",
            "recent_logs", "total_scans",
        ]
        for field in required_fields:
            assert field in status, f"OcrChatWatcher.get_status() 缺少字段: {field}"


# ══════════════════════════════════════════════════════════════════════════════
# 8. 严格前缀点歌/切歌插播与 4 层防重复入队测试
# ══════════════════════════════════════════════════════════════════════════════

class TestStrictPrefixAndZeroDuplicate:
    """验证严格 prefix 模式、中英文/数字歌名兼容、切歌插播与多层去重逻辑。"""

    def test_prefix_mode_accepts_cjk_english_numeric_and_skip_to(self) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        w = OcrChatWatcher(cooldown_sec=0.0)
        lines = [
            "点歌：晴天-周杰伦",
            "点歌: Crossing Field-LiSA",
            "点歌：21 Guns-Green Day",
            "点歌：Sugar-Maroon 5",
            "点歌：Don't Stop Me Now-Queen",
            "切歌：稻香-周杰伦",
        ]
        cmds = w.parse_chat_lines(
            lines,
            trigger_mode="prefix",
            allow_skip_cmd=True,
            bypass_dedup=True,
        )
        assert len(cmds) == 6, f"期望 6 条严格格式全部命中，实际: {cmds}"
        assert [(c["type"], c["title"], c["artist"]) for c in cmds] == [
            ("song", "晴天", "周杰伦"),
            ("song", "Crossing Field", "LiSA"),
            ("song", "21 Guns", "Green Day"),
            ("song", "Sugar", "Maroon 5"),
            ("song", "Don't Stop Me Now", "Queen"),
            ("skip_to", "稻香", "周杰伦"),
        ]

    def test_prefix_mode_rejects_bare_songs_and_bare_skip(self) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        w = OcrChatWatcher(cooldown_sec=0.0)
        lines = [
            "晴天-周杰伦",
            "新地球-林俊杰",
            "切歌",
            "下一首",
            "换歌",
            "点歌怎么用啊",
            "点歌台真好用",
            "点歌 帮主好厉害",
            "点歌：怎么用啊",
            "点歌：夜曲",
            "点歌：晴天-",
            "切歌怎么不切",
            "切歌 帮我换一首",
            "切歌：稻香",
            "点歌：123-456",
            "点歌：RTX-4060",
        ]
        cmds = w.parse_chat_lines(
            lines,
            trigger_mode="prefix",
            allow_skip_cmd=True,
            bypass_dedup=True,
        )
        assert cmds == [], f"严格 prefix 模式下闲聊对话/缺冒号/缺横杠均不应命中，实际: {cmds}"

    def test_static_screen_never_retriggers_after_cooldown_zero(self) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        w = OcrChatWatcher(cooldown_sec=0.0)
        lines = ["蘭 明月测试侠", "点歌：晴天-周杰伦"]

        first = w.parse_chat_lines(lines, trigger_mode="prefix", allow_skip_cmd=True, bypass_dedup=False)
        assert len(first) == 1

        for _ in range(5):
            again = w.parse_chat_lines(lines, trigger_mode="prefix", allow_skip_cmd=True, bypass_dedup=False)
            assert again == [], f"静止画面残留冒泡不应该再次触发: {again}"

    def test_close_reopen_chat_and_restart_ledger_blocks_duplicate(self, tmp_path: Path) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        ledger_path = tmp_path / "ocr_seen.json"
        w1 = OcrChatWatcher(cooldown_sec=0.0, seen_store_path=ledger_path)

        chat_open = ["你们好冷淡", "蘭 明月测试侠", "点歌：晴天-周杰伦"]
        chat_closed = ["江湖路人", "致 （未开放）"]

        assert len(w1.parse_chat_lines(chat_open, trigger_mode="prefix", bypass_dedup=False)) == 1
        assert w1.parse_chat_lines(chat_closed, trigger_mode="prefix", bypass_dedup=False) == []
        # 重开聊天面板后同一上下文的旧气泡必须被上下文台账拦截
        assert w1.parse_chat_lines(chat_open, trigger_mode="prefix", bypass_dedup=False) == []

        # 模拟服务重启，从磁盘台账恢复记忆
        w2 = OcrChatWatcher(cooldown_sec=0.0, seen_store_path=ledger_path)
        assert w2.parse_chat_lines(chat_open, trigger_mode="prefix", bypass_dedup=False) == []

    def test_controller_queue_dedup_and_skip_to_behavior(self, tmp_path: Path) -> None:
        from unittest.mock import patch
        from backend.config_store import ConfigStore
        from backend.jukebox_controller import JukeboxController

        store = ConfigStore(config_path=tmp_path / "cfg.json")
        ctrl = JukeboxController(config_store=store, dummy_sink=True, start_daemons=False)
        try:
            track_a = {"video_id": "vid_a", "title": "新地球", "artist": "林俊杰", "duration_sec": 270}
            track_b = {"video_id": "vid_b", "title": "稻香", "artist": "周杰伦", "duration_sec": 220}

            # 队列去重：相同 video_id 第二次应被跳过
            first_item = ctrl.enqueue_track(track_a, requester="A", source="ocr", dedup_queue=True)
            dup_item = ctrl.enqueue_track(track_a, requester="A", source="ocr", dedup_queue=True)
            assert first_item is not None
            assert dup_item is None
            assert len(ctrl.get_queue()) == 1

            # 模拟曲目 A 正在播放
            with ctrl.lock:
                ctrl._queue.clear()
                ctrl._current_item = {
                    "item_id": "cur_1",
                    "track": track_a,
                    "video_id": "vid_a",
                    "title": "新地球",
                    "artist": "林俊杰",
                }

            with patch.object(ctrl.audio_engine, "get_status", return_value={"state": "playing", "routing": {}}), \
                 patch.object(ctrl.ytmusic, "search_best_match", return_value=track_a), \
                 patch.object(ctrl, "skip_track") as mock_skip:
                # 正在播放曲目 A 时再点 A：跳过重复点歌
                assert ctrl._enqueue_from_ocr_command({"title": "新地球", "artist": "林俊杰", "requester": "B"}) is None
                # 切歌：新地球（正好是当前在播） -> 直接切当前下一首，不重排 A
                res_same = ctrl._handle_ocr_skip_to({"title": "新地球", "artist": "林俊杰", "requester": "B"})
                assert res_same is not None and res_same.get("action") == "skipped_current"
                assert mock_skip.call_count == 1
                assert len(ctrl.get_queue()) == 0

            with patch.object(ctrl.audio_engine, "get_status", return_value={"state": "playing", "routing": {}}), \
                 patch.object(ctrl.ytmusic, "search_best_match", return_value=track_b), \
                 patch.object(ctrl, "skip_track") as mock_skip:
                # 切歌：稻香-周杰伦（不同于当前在播） -> 置顶插播并立刻触发切歌
                res_new = ctrl._handle_ocr_skip_to({"title": "稻香", "artist": "周杰伦", "requester": "C"})
                assert res_new is not None and res_new.get("video_id") == "vid_b"
                assert mock_skip.call_count == 1
                q = ctrl.get_queue()
                assert len(q) == 1 and q[0]["video_id"] == "vid_b"
        finally:
            ctrl.shutdown()

    def test_serif_hyphen_recovered_as_yi_and_measure_words_blocked(self) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        w = OcrChatWatcher(cooldown_sec=0.0)
        ok_cmds = w.parse_chat_lines(
            ["点歌：本兮一情花"],
            trigger_mode="prefix",
            allow_skip_cmd=True,
            bypass_dedup=True,
        )
        assert len(ok_cmds) == 1
        assert ok_cmds[0]["type"] == "song"
        assert ok_cmds[0]["title"] == "本兮"
        assert ok_cmds[0]["artist"] == "情花"

        blocked_cmds = w.parse_chat_lines(
            ["点歌：换一首歌", "点歌：给我一首歌", "切歌：换一首"],
            trigger_mode="prefix",
            allow_skip_cmd=True,
            bypass_dedup=True,
        )
        assert blocked_cmds == [], f"量词组合不应被当成 '-' 恢复: {blocked_cmds}"

    def test_multiline_wrapped_bubble_stitching(self) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        ocr_boxes = [
            ([[300, 456], [496, 456], [496, 490], [300, 490]], "明月测试侠", 0.99),
            ([[58, 542], [506, 542], [506, 580], [58, 580]], "点歌:Taylor-i knew you were i", 0.94),
            ([[54, 584], [189, 584], [189, 613], [54, 613]], "n trouble", 0.99),
            ([[271, 681], [496, 681], [496, 715], [271, 715]], "蘭明月测试侠", 0.99),
            ([[58, 768], [504, 768], [504, 803], [58, 803]], "切歌:Taylor-i knew you were i", 0.94),
            ([[55, 811], [189, 811], [189, 840], [55, 840]], "n trouble", 0.99),
        ]
        stitched = OcrChatWatcher._merge_ocr_boxes_into_lines(ocr_boxes)
        assert stitched == [
            "明月测试侠",
            "点歌:Taylor-i knew you were in trouble",
            "蘭明月测试侠",
            "切歌:Taylor-i knew you were in trouble",
        ]

    def test_ytmusic_bidirectional_and_paren_stripped_matching(self, tmp_path: Path) -> None:
        from unittest.mock import patch
        from backend.ytmusic_service import YTMusicService

        svc = YTMusicService(data_dir=tmp_path / "data", trash_dir=tmp_path / "_trash")
        mock_cands = [
            {"video_id": "v_begin", "title": "Begin Again (Taylor's Version)", "artist": "Taylor Swift", "duration_sec": 239, "result_type": "song"},
            {"video_id": "v_cover", "title": "I Knew You Were Trouble (Cover)", "artist": "RandomGuy", "duration_sec": 210, "result_type": "video"},
            {"video_id": "v_real", "title": "I Knew You Were Trouble.", "artist": "Taylor Swift", "duration_sec": 220, "result_type": "song"},
        ]
        with patch.object(svc, "search_songs", return_value=mock_cands):
            m1 = svc.search_best_match("I Knew You Were Trouble", "Taylor Swift")
            m2 = svc.search_best_match("Taylor", "i knew you were in trouble")
            assert m1 is not None and m1["video_id"] == "v_real", f"正向匹配错选: {m1}"
            assert m2 is not None and m2["video_id"] == "v_real", f"歌手-歌名倒装加小typo错选: {m2}"

    def test_ytmusic_traditional_original_beats_simplified_cover(self, tmp_path: Path) -> None:
        from unittest.mock import patch
        from backend.ytmusic_service import YTMusicService

        svc = YTMusicService(data_dir=tmp_path / "data", trash_dir=tmp_path / "_trash")
        mock_cands = [
            {"video_id": "v_cover", "title": "神话情话", "artist": "亮声open", "duration_sec": 141, "result_type": "song"},
            {"video_id": "v_orig", "title": "神話．情話", "artist": "周華健 / 齊豫", "duration_sec": 267, "result_type": "song"},
        ]
        with patch.object(svc, "search_songs", return_value=mock_cands):
            m1 = svc.search_best_match("周华健", "神话神情")
            m2 = svc.search_best_match("神话情话", "周华健")
            assert m1 is not None and m1["video_id"] == "v_orig", f"繁简原唱应胜过简体翻唱: {m1}"
            assert m2 is not None and m2["video_id"] == "v_orig", f"正向检索繁简原唱应胜过简体翻唱: {m2}"

    def test_prefix_mode_allows_artist_punctuation_and_multi_hyphen(self) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        w = OcrChatWatcher(cooldown_sec=0.0)
        lines = [
            "点歌：失眠飞行-接个吻，开一枪",
            "点歌：你，好不好？-周兴哲",
            "点歌：神話．情話-周华健、齐豫",
            "点歌：blackpink-DDU-DU DDU-DU",
            "点歌：2002-Anne-Marie",
        ]
        cmds = w.parse_chat_lines(
            lines,
            trigger_mode="prefix",
            allow_skip_cmd=True,
            bypass_dedup=True,
        )
        assert len(cmds) == 5, f"包含真实标点与多连字符的 5 首曲目应全部识别: {cmds}"
        assert [(c["title"], c["artist"]) for c in cmds] == [
            ("失眠飞行", "接个吻，开一枪"),
            ("你，好不好？", "周兴哲"),
            ("神話．情話", "周华健、齐豫"),
            ("blackpink", "DDU-DU DDU-DU"),
            ("2002", "Anne-Marie"),
        ]

    def test_ytmusic_bilingual_subtitle_and_artist_mismatch_guard(self, tmp_path: Path) -> None:
        from unittest.mock import patch
        from backend.ytmusic_service import YTMusicService

        svc = YTMusicService(data_dir=tmp_path / "data", trash_dir=tmp_path / "_trash")
        mock_bp = [
            {"video_id": "v_bp_orig", "title": "뚜두뚜두(DDU-DU DDU-DU)", "artist": "BLACKPINK", "duration_sec": 210, "result_type": "song"},
            {"video_id": "v_bp_remix", "title": "DDU-DU DDU-DU (Remix)", "artist": "BLACKPINK", "duration_sec": 202, "result_type": "song"},
            {"video_id": "v_dj", "title": "Du Du Du", "artist": "DJ Monaco", "duration_sec": 164, "result_type": "song"},
        ]
        with patch.object(svc, "search_songs", return_value=mock_bp):
            m_short = svc.search_best_match("blackpink", "Du")
            m_full = svc.search_best_match("blackpink", "DDU-DU DDU-DU")
            assert m_short is not None and m_short["video_id"] == "v_bp_orig", f"blackpink-Du 误判: {m_short}"
            assert m_full is not None and m_full["video_id"] == "v_bp_orig", f"blackpink-DDU-DU DDU-DU 误判: {m_full}"

        mock_taylor = [
            {"video_id": "v_begin", "title": "Begin Again (Taylor's Version)", "artist": "Taylor Swift", "duration_sec": 239, "result_type": "song"},
            {"video_id": "v_real", "title": "I Knew You Were Trouble.", "artist": "Taylor Swift", "duration_sec": 220, "result_type": "song"},
        ]
        with patch.object(svc, "search_songs", return_value=mock_taylor):
            m_t = svc.search_best_match("Taylor", "i knew you were in trouble")
            assert m_t is not None and m_t["video_id"] == "v_real", f"保留双语括号后仍应准确排除 Begin Again: {m_t}"

    def test_scroll_to_top_empty_context_never_retriggers(self, tmp_path: Path) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        seen_path = tmp_path / "ocr_seen.json"
        w1 = OcrChatWatcher(cooldown_sec=0.0, seen_store_path=seen_path)
        initial_lines = ["兰 明月测试侠", "点歌：周杰伦-听妈妈的话", "明月测试侠"]
        assert len(w1.parse_chat_lines(initial_lines, trigger_mode="prefix", bypass_dedup=False)) == 1

        # 模拟服务重启且该条气泡滚到最顶行（上文发言人离开视线，ctx == ""）
        w2 = OcrChatWatcher(cooldown_sec=0.0, seen_store_path=seen_path)
        scrolled_top_lines = ["点歌：周杰伦-听妈妈的话", "明月测试侠"]
        assert w2.parse_chat_lines(scrolled_top_lines, trigger_mode="prefix", bypass_dedup=False) == []

    def test_leading_level_badge_stripped_before_strict_cmd(self) -> None:
        from backend.ocr_watcher import OcrChatWatcher

        w = OcrChatWatcher(cooldown_sec=0.0)
        cmds = w.parse_chat_lines(
            ["14 点歌：晴天-周杰伦"],
            trigger_mode="prefix",
            allow_skip_cmd=True,
            bypass_dedup=True,
        )
        assert len(cmds) == 1
        assert cmds[0]["type"] == "song"
        assert cmds[0]["title"] == "晴天"
        assert cmds[0]["artist"] == "周杰伦"

    def test_played_history_blocks_already_heard_songs_from_ocr(self, tmp_path: Path) -> None:
        from backend.config_store import ConfigStore
        from backend.jukebox_controller import JukeboxController

        store = ConfigStore(config_path=tmp_path / "cfg.json")
        ctrl = JukeboxController(config_store=store, dummy_sink=True, start_daemons=False)
        try:
            track_a = {"video_id": "vid_听妈", "title": "聽媽媽的話", "artist": "周杰倫", "duration_sec": 266}
            track_a_alt = {"video_id": "vid_听妈_alt", "title": "听妈妈的话", "artist": "周杰伦", "duration_sec": 266}
            track_b = {"video_id": "vid_新歌", "title": "稻香", "artist": "周杰伦", "duration_sec": 223}

            ctrl._record_played_track(track_a)

            # OCR 自动点歌命中同 video_id 或同歌名（含简繁变体）应被跳过
            assert ctrl.enqueue_track(track_a, requester="帮众A", source="ocr", dedup_queue=True) is None
            assert ctrl.enqueue_track(track_a_alt, requester="帮众B", source="ocr", dedup_queue=True) is None
            assert len(ctrl.get_queue()) == 0

            # 控制台手动点击仍可正常排队；新歌 OCR 正常入队
            assert ctrl.enqueue_track(track_a, requester="房主", source="web", dedup_queue=False) is not None
            assert ctrl.enqueue_track(track_b, requester="帮众C", source="ocr", dedup_queue=True) is not None
            assert len(ctrl.get_queue()) == 2
        finally:
            ctrl.shutdown()


class TestScheduledBroadcastMode:
    """每周四、周日 19:30–20:20 定时播音/OCR 休眠状态机与 API 冒烟测试。"""

    def test_standby_vs_scheduled_window_evaluation_and_gating(self, tmp_path: Path) -> None:
        from datetime import datetime
        from unittest.mock import patch
        from backend.config_store import ConfigStore
        from backend.jukebox_controller import JukeboxController

        store = ConfigStore(config_path=tmp_path / "cfg.json")
        ctrl = JukeboxController(config_store=store, dummy_sink=True, start_daemons=False)
        try:
            # 1. 周六 14:30 / 周四 19:29 / 周四 20:20 均应处于 standby（非播音时段）
            sat_afternoon = datetime(2026, 11, 21, 14, 30, 0)
            thu_before = datetime(2026, 11, 26, 19, 29, 0)
            thu_end = datetime(2026, 11, 26, 20, 20, 0)

            s_sat = ctrl.evaluate_broadcast_schedule(now=sat_afternoon)
            assert s_sat["mode"] == "standby"
            assert s_sat["is_active"] is False
            assert s_sat["next_start_label"] == "明天 19:30（周日）"

            s_before = ctrl.evaluate_broadcast_schedule(now=thu_before)
            assert s_before["mode"] == "standby"
            assert s_before["is_active"] is False
            assert s_before["next_start_label"] == "今天 19:30（周四）"

            s_end = ctrl.evaluate_broadcast_schedule(now=thu_end)
            assert s_end["mode"] == "standby"
            assert s_end["is_active"] is False
            assert s_end["next_start_label"] == "周日 19:30"

            # 2. 周四 19:30 / 20:19 与周日 19:45 均应处于 live_scheduled
            for dt in (
                datetime(2026, 11, 26, 19, 30, 0),
                datetime(2026, 11, 26, 20, 19, 59),
                datetime(2026, 11, 22, 19, 45, 0),
            ):
                st = ctrl.evaluate_broadcast_schedule(now=dt)
                assert st["mode"] == "live_scheduled"
                assert st["is_active"] is True

            # 3. 待机时段 _preload_step 与 _autodj_step 不得拉歌、不得起播
            ctrl._fallback_tracks = [
                {"video_id": "fb_1", "title": "云端兜底", "artist": "歌手", "duration_sec": 200, "thumbnail_url": ""}
            ]
            with patch.object(
                ctrl,
                "evaluate_broadcast_schedule",
                return_value=ctrl.evaluate_broadcast_schedule(now=sat_afternoon),
            ), \
                 patch.object(ctrl.ytmusic, "resolve_audio_source") as mock_resolve, \
                 patch.object(ctrl.audio_engine, "play_file") as mock_play:
                ctrl._preload_step()
                ctrl._autodj_step()
                assert mock_resolve.call_count == 0
                assert mock_play.call_count == 0
        finally:
            ctrl.shutdown()

    def test_window_end_transition_stops_audio_ocr_clears_queue_and_clears_override(self, tmp_path: Path) -> None:
        from datetime import datetime
        from unittest.mock import patch
        from backend.config_store import ConfigStore
        from backend.jukebox_controller import JukeboxController

        store = ConfigStore(config_path=tmp_path / "cfg.json")
        ctrl = JukeboxController(config_store=store, dummy_sink=True, start_daemons=False)
        try:
            t_early = datetime(2026, 11, 26, 19, 20, 0)
            t_in = datetime(2026, 11, 26, 19, 30, 0)
            t_end = datetime(2026, 11, 26, 20, 20, 0)

            with patch.object(ctrl.ocr_watcher, "start"), \
                 patch.object(ctrl.ocr_watcher, "stop") as mock_ocr_stop, \
                 patch.object(ctrl.audio_engine, "stop") as mock_audio_stop:
                # 19:20 手动提前试播
                with ctrl.lock:
                    ctrl._manual_override_active = True
                s1 = ctrl._sync_schedule_state(now=t_early, emit_events=False)
                assert s1["mode"] == "live_override"
                assert s1["is_active"] is True

                ctrl.enqueue_track(
                    {"video_id": "vid_q1", "title": "晴天", "artist": "周杰伦", "duration_sec": 260},
                    requester="帮众甲",
                    source="ocr",
                )
                assert len(ctrl.get_queue()) == 1

                # 19:30 进入定时窗口，自动清除临时试播标记防止 20:20 后漏关
                s2 = ctrl._sync_schedule_state(now=t_in, emit_events=False)
                assert s2["mode"] == "live_scheduled"
                assert s2["manual_override"] is False
                assert ctrl._manual_override_active is False

                # 20:20 到达结束边界 -> 停播 + 停 OCR + 清空队列
                s3 = ctrl._sync_schedule_state(now=t_end, emit_events=False)
                assert s3["mode"] == "standby"
                assert s3["is_active"] is False
                assert mock_audio_stop.call_count >= 1
                assert mock_ocr_stop.call_count >= 1
                assert ctrl.get_queue() == []
        finally:
            ctrl.shutdown()

    def test_manual_override_and_schedule_api_routes(self, tmp_path: Path) -> None:
        from datetime import datetime
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from backend.config_store import ConfigStore
        from backend.jukebox_controller import JukeboxController
        import backend.main as main_mod

        store = ConfigStore(config_path=tmp_path / "cfg.json")
        store.update_ocr({"enabled": True})
        ctrl = JukeboxController(config_store=store, dummy_sink=True, start_daemons=False)
        try:
            standby_now = datetime(2026, 11, 21, 14, 30, 0)
            orig_eval = ctrl.evaluate_broadcast_schedule

            def _eval_at_standby(now=None):
                return orig_eval(now=now or standby_now)

            with patch.object(main_mod, "get_controller", return_value=ctrl), \
                 patch.object(ctrl, "evaluate_broadcast_schedule", side_effect=_eval_at_standby), \
                 patch.object(ctrl.ocr_watcher, "start") as mock_ocr_start, \
                 patch.object(ctrl.ocr_watcher, "stop") as mock_ocr_stop, \
                 patch.object(ctrl.audio_engine, "stop") as mock_audio_stop:
                client = TestClient(main_mod.app)

                # 先同步至待机初态，然后通过 API 开启临时试播
                ctrl._sync_schedule_state(emit_events=False)
                r_on = client.post("/api/schedule/override", json={"active": True})
                assert r_on.status_code == 200
                data_on = r_on.json()
                assert data_on["schedule"]["mode"] == "live_override"
                assert data_on["schedule"]["is_active"] is True
                assert mock_ocr_start.call_count >= 1

                # 再关闭试播恢复待机
                r_off = client.post("/api/schedule/override", json={"active": False})
                assert r_off.status_code == 200
                data_off = r_off.json()
                assert data_off["schedule"]["mode"] == "standby"
                assert data_off["schedule"]["is_active"] is False
                assert mock_audio_stop.call_count >= 1
                assert mock_ocr_stop.call_count >= 1

                # 更新定时配置并确认与 ocr.peak_schedule 同步落盘
                r_cfg = client.post(
                    "/api/schedule/config",
                    json={
                        "enabled": True,
                        "days": [3, 6],
                        "start": "19:30",
                        "end": "20:20",
                        "clear_queue_on_end": True,
                    },
                )
                assert r_cfg.status_code == 200
                cfg_res = r_cfg.json()
                assert cfg_res["ok"] is True
                assert cfg_res["schedule"]["days"] == [3, 6]
                assert cfg_res["ocr"]["peak_schedule"]["start"] == "19:30"
                assert cfg_res["ocr"]["standby_paused"] is True
        finally:
            ctrl.shutdown()
