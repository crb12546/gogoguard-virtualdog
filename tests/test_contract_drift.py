"""漂移哨兵：虚拟狗 vendored 的 go2_protocol 必须与平台契约【逐字一致】。
拆开后狗不再和平台共享 go2_protocol 代码，靠这个测试防止两边悄悄漂。
平台仓在已知 sibling 路径时逐字对比；不在(换机/CI)则跳过——那种环境用"对平台发字节"的对拍替代。"""
import hashlib
import pathlib
import pytest

DOG_ROOT = pathlib.Path(__file__).resolve().parent.parent
MINE = DOG_ROOT / "go2_protocol.py"
# 平台契约（同机 sibling 仓）。换路径就改这里。
PLATFORM = pathlib.Path.home() / "Projects" / "llyj" / "backend" / "go2_protocol.py"


def test_vendored_contract_matches_platform():
    assert MINE.exists(), "狗里缺 go2_protocol.py（vendored 契约副本）"
    if not PLATFORM.exists():
        pytest.skip(f"平台契约不在 {PLATFORM}（换机/CI）→ 改用对平台发字节的对拍验证")
    a = hashlib.md5(MINE.read_bytes()).hexdigest()
    b = hashlib.md5(PLATFORM.read_bytes()).hexdigest()
    assert a == b, ("虚拟狗 go2_protocol 与平台契约已漂移！\n"
                    f"  狗:   {MINE}\n  平台: {PLATFORM}\n"
                    "改契约后两边同步 go2_protocol.py 再跑。")
