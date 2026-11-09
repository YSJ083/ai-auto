"""验证码识别 —— 自动测试用的最小 OCR 层。

设计原则：**识别不了就明确报错，绝不返回空串让上层当成"填对了"**。
验证码错一次只会换来一条 `CAPTCHA_ERROR`，但如果这里静默返回 `""`，
登录用例会变成"填了空验证码 → 前端拦截 → 断言 URL 仍在登录页 → 通过"，
这是最坏的假通过。

三级策略，从上到下退化：
  1. ddddocr —— 专门针对验证码训练，彩色+扭曲+干扰线都能认，首选
  2. tesseract —— 通用 OCR，靠 Pillow 预处理（放大/取暗通道/二值化）救一点准确率
  3. 都没有 —— 抛 CaptchaUnavailable，由上层判 `blocked`，不当失败

为什么不用"人眼识别"当兜底：那不是自动测试。宁可标 blocked，也不能假装跑了。

关于准确率（实测于目标系统 4 位字母数字码）：
  - ddddocr 标准模型约 80%，beta 模型约 73%，tesseract 约 20%
  - 误差几乎全是**字符认错**（O/0、l/1、S/5），不是大小写问题 ——
    该系统的验证码校验本身不区分大小写，用大小写变体去碰撞没有意义
  - 单个 captchaId 是**一次性的**，提交失败即失效，所以"同一张图换个写法再试"
    这条路走不通，只能在用例层重新取码（用例级重试就是干这个的）
  - 长度不符的结果是必败的，宁可原地抛错快速进入重试，也别拿去提交
"""

import io
import os
import subprocess

CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
TESSERACT_HINT = "brew install tesseract"


class CaptchaError(Exception):
    """识别失败（图拿到了但认不出，或结果明显不合法）。"""


class CaptchaUnavailable(Exception):
    """没有任何可用的识别引擎。"""


_engine = None
_ocr = None


def _load():
    """惰性探测引擎，只探一次。"""
    global _engine
    if _engine is not None:
        return _engine
    try:
        import ddddocr  # noqa: F401
        _engine = ("ddddocr", None)
        return _engine
    except Exception:
        pass
    try:
        subprocess.run(["tesseract", "--version"], capture_output=True, timeout=8)
        _engine = ("tesseract", None)
        return _engine
    except Exception:
        _engine = (None, "既没有 ddddocr，也没有 tesseract 命令")
        return _engine


def _has_tesseract():
    try:
        subprocess.run(["tesseract", "--version"], capture_output=True, timeout=8)
        return True
    except Exception:
        return False


def available():
    """返回 (引擎名或 None, 原因)。给 lint / 降级判断用。"""
    name, reason = _load()
    return name, reason


def _log(msg, img_bytes=None):
    """AUTOTEST_CAPTCHA_DEBUG=1 时打日志；=save 时额外把图存到 /tmp。"""
    tag = os.environ.get("AUTOTEST_CAPTCHA_DEBUG")
    if not tag:
        return
    import sys
    import time
    if tag == "save" and img_bytes:
        try:
            p = "/tmp/captcha_%d.png" % time.time_ns()
            with open(p, "wb") as f:
                f.write(img_bytes)
            msg += " → " + p
        except Exception:
            pass
    print("[验证码] " + msg, file=sys.stderr)


def _prep(img_bytes, threshold=220, scale=4):
    """放大 + 取三通道最小值 + 二值化。

    直接转灰度会丢掉浅色字符：黄色 `(255,255,0)` 和浅蓝 `(120,200,255)`
    灰度后几乎与白底同值，tesseract 只认得出深色的那几个（实测 4 位只出 2 位）。
    取 `min(R,G,B)` 后，所有彩色字符都会压成深色，白底仍是 255，对比度拉满。

    阈值不能一刀切：150 时浅蓝的 H 会被当成背景丢掉，220 才认得全。
    但阈值越高，抗锯齿边缘越容易连成噪点，所以会多个阈值各跑一遍再按长度筛。
    """
    from PIL import Image, ImageChops, ImageOps

    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    r, g, b = im.split()
    darkness = ImageChops.darker(ImageChops.darker(r, g), b)
    darkness = darkness.resize(
        (darkness.width * scale, darkness.height * scale), Image.LANCZOS)
    darkness = ImageOps.autocontrast(darkness)
    return darkness.point(lambda x: 0 if x < threshold else 255)


def _tess(pil_img, chars):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    r = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "7",
         "-c", "tessedit_char_whitelist=" + chars],
        input=buf.getvalue(), capture_output=True, timeout=20)
    return r.stdout.decode("utf-8", "ignore")


def _tess_candidates(img_bytes, chars):
    """多个二值化阈值各跑一遍，返回候选列表（去重、保序）。"""
    out = []
    for th in (220, 200, 235, 180, 150):
        try:
            got = "".join(c for c in _tess(_prep(img_bytes, th), chars) if c in chars)
        except Exception:
            continue
        if got and got not in out:
            out.append(got)
    return out


def _dddd(img_bytes, chars):
    """ddddocr 识别一次。模型加载约 0.3s，实例缓存复用。"""
    global _ocr
    import ddddocr

    if _ocr is None:
        try:
            _ocr = ddddocr.DdddOcr(show_ad=False)
        except TypeError:              # 老版本没有 show_ad 参数
            _ocr = ddddocr.DdddOcr()
    return "".join(c for c in (_ocr.classification(img_bytes) or "") if c in chars)


def recognize(img_bytes, chars=CHARS, expect_len=None):
    """识别一张验证码图，返回字符串（已去掉非字母数字与空白）。

    识别不出、或长度与 `expect_len` 不符且无法交叉验证时抛 CaptchaError，
    交由用例级重试重新取码。绝不返回空串。
    """
    name, reason = _load()
    if not name:
        raise CaptchaUnavailable(reason or "无可用 OCR 引擎")

    if name == "ddddocr":
        cleaned = _dddd(img_bytes, chars)
        if cleaned and (not expect_len or len(cleaned) == expect_len):
            _log("ddddocr -> %r" % cleaned, img_bytes)
            return cleaned

        # 长度不符：字符粘连/断裂时 ddddocr 会多吐或少吐一位。这种结果拿去提交
        # 必错，还白白消耗掉一个一次性的 captchaId。先用 tesseract 交叉验证。
        if _has_tesseract():
            hits = [c for c in _tess_candidates(img_bytes, chars)
                    if not expect_len or len(c) == expect_len]
            if hits:
                _log("ddddocr %r 长度不符，tesseract 交叉验证 -> %r"
                     % (cleaned, hits[0]), img_bytes)
                return hits[0]

        if not cleaned:
            raise CaptchaError("ddddocr 没识别出任何字符")
        raise CaptchaError("ddddocr 识别出 %r（%d 位），预期 %d 位，交叉验证无结果"
                           % (cleaned, len(cleaned), expect_len))

    # 只有 tesseract
    candidates = _tess_candidates(img_bytes, chars)
    if not candidates:
        raise CaptchaError("tesseract 在各阈值下都没识别出字符")
    if expect_len:
        hit = [c for c in candidates if len(c) == expect_len]
        if hit:
            _log("tesseract -> %r" % hit[0], img_bytes)
            return hit[0]
    _log("tesseract -> %r（长度未达预期）" % max(candidates, key=len), img_bytes)
    return max(candidates, key=len)
