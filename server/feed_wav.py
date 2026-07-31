"""保存済み WAV を、実際の WebSocket 経由で稼働中のサーバーへ流し込む。

プラグインの代わりをするテスト用クライアント。マイクも部屋も介さないので、
**同じ音声を何度でも同じ条件で**サーバーに通せる。用途:

  - 設定やコードを変えたときの前後比較（幻覚の件数、段落の粒度、⟨未認識⟩の数）
  - 「サーバーが正しいのか、端末側が正しいのか」の切り分け
  - 稼働中のプロセスそのものの検証（モジュールを import する検証とは別物。
    再起動忘れ・環境変数の効き忘れもここで分かる）

クライアント（main.ts）の挙動も真似る: チャンクの隙間が 0.35 秒以上あれば
⟨未認識 …⟩ を挟む。つまり**ノートに入るはずの本文がそのまま出力される**。

使い方
------
  python feed_wav.py 20260730-120141                    # 実時間で流す
  python feed_wav.py 20260730-120141 --limit 90         # 先頭90秒だけ
  python feed_wav.py path/to/any.wav --out result.md    # 本文をファイルへ
  python feed_wav.py 20260730-133249 --mode dictation   # 口述モードで流す

注意
----
- 既定は**実時間**で送る（--fast で最速送出）。文字起こしモードのチャンク連結は
  実時間で待ち時間を測るので、早送りすると連結の挙動が本番と変わる。比較目的なら
  実時間のままにすること。
- 文字起こしモードで流すと、サーバーは受け取った音声を新しい録音として保存する。
  元ファイルの複製が増えるので、不要なら最後に出る session ID を消すこと。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

RECORDINGS = Path(__file__).resolve().parent / "recordings"

BLOCK_SEC = 0.1          # 1回に送る音声の長さ（プラグインの送出間隔に合わせた粒度）
GAP_SEC = 0.35           # これ以上の空きは ⟨未認識⟩ 扱い（main.ts と同じ値）


def resolve_wav(name: str) -> Path:
    """セッションID でも WAV のパスでも受け取れるようにする。"""
    p = Path(name)
    if p.suffix.lower() == ".wav" and p.exists():
        return p
    candidate = RECORDINGS / f"{name}.wav"
    if candidate.exists():
        return candidate
    raise SystemExit(f"WAV が見つかりません: {name}")


def read_pcm(path: Path, start_sec: float, limit_sec: float | None) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("モノラル16bitのWAVだけ扱えます")
        sr = w.getframerate()
        w.setpos(min(int(start_sec * sr), w.getnframes()))
        n = w.getnframes() - w.tell()
        if limit_sec is not None:
            n = min(n, int(limit_sec * sr))
        return w.readframes(n), sr


def fmt_time(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


async def run(args: argparse.Namespace) -> int:
    try:
        import websockets
    except ImportError:
        raise SystemExit("websockets が要ります: .venv\\Scripts\\pip install websockets")

    path = resolve_wav(args.session)
    pcm, sr = read_pcm(path, args.start, args.limit)
    total_sec = len(pcm) / 2 / sr
    print(f"# {path.name} {args.start:.0f}秒から {total_sec:.1f}秒 ({sr}Hz) → {args.url}")
    print(f"# モード={args.mode} 送出={'最速' if args.fast else '実時間'}")

    body: list[str] = []          # ノートに入るはずの本文
    stats = {"chunk": 0, "empty": 0, "dropped": 0, "gap": 0}
    session_id: str | None = None
    last_end = 0.0
    started = time.monotonic()

    async with websockets.connect(args.url, max_size=None) as ws:

        async def receive() -> None:
            nonlocal session_id, last_end
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "session":
                    session_id = msg.get("session")
                    print(f"# サーバー側の録音セッション: {session_id}")
                elif kind == "chunk":
                    text = msg.get("text") or ""
                    start, end = msg.get("start"), msg.get("end")
                    # クライアントと同じ規則で欠落マーカーを挟む。
                    if start is not None and last_end > 0 and start - last_end >= GAP_SEC:
                        marker = f"⟨未認識 {fmt_time(last_end)}–{fmt_time(start)}⟩"
                        body.append(marker)
                        stats["gap"] += 1
                        print(f"  {marker}")
                    if text:
                        stats["chunk"] += 1
                        body.append(text)
                        head = text.replace("\n\n", " ⏎⏎ ")
                        pause = msg.get("pause")
                        print(f"  [{start}-{end} pause={pause}] {head}")
                    else:
                        stats["empty"] += 1
                    if msg.get("dropped"):
                        stats["dropped"] += len(msg["dropped"])
                        print(f"  ※ 破棄 {msg['dropped']}")
                    if end is not None:
                        last_end = end
                elif kind == "error":
                    print(f"  !! サーバーからのエラー: {msg.get('message')}")
                elif kind == "stopped":
                    return

        receiver = asyncio.create_task(receive())
        await ws.send(json.dumps({"type": "start", "mode": args.mode}))

        block = int(BLOCK_SEC * sr) * 2  # bytes
        sent = 0
        while sent < len(pcm):
            await ws.send(pcm[sent:sent + block])
            sent += block
            if not args.fast:
                # 実時間に追いつくまで待つ（送りすぎない）。
                target = started + (sent / 2 / sr)
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

        await ws.send(json.dumps({"type": "stop"}))
        try:
            await asyncio.wait_for(receiver, timeout=args.timeout)
        except asyncio.TimeoutError:
            print(f"# 停止の応答が {args.timeout} 秒以内に来ませんでした（認識が詰まっている可能性）")
            receiver.cancel()

    text = "".join(body)
    paragraphs = [p for p in text.split("\n\n") if p]
    print(f"\n# チャンク {stats['chunk']} / 空 {stats['empty']} / 破棄 {stats['dropped']} "
          f"/ ⟨未認識⟩ {stats['gap']}")
    print(f"# 本文 {len(text)}字 / 段落 {len(paragraphs)}"
          + (f" / 平均 {sum(len(p) for p in paragraphs) // len(paragraphs)}字" if paragraphs else ""))
    for word in ("ご視聴", "チャンネル登録", "お楽しみに"):
        n = text.count(word)
        if n:
            print(f"# 本文に残った「{word}」: {n}件")
    if session_id:
        print(f"# 複製された録音: recordings/{session_id}.wav （不要なら削除）")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"# 本文を書き出しました: {args.out}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("session", help="セッションID（recordings/ 配下）または WAV のパス")
    ap.add_argument("--url", default="ws://127.0.0.1:8760/ws")
    ap.add_argument("--mode", default="transcribe", choices=["transcribe", "dictation"])
    ap.add_argument("--start", type=float, default=0.0, help="開始位置（秒）")
    ap.add_argument("--limit", type=float, default=None, help="流す長さ（秒）")
    ap.add_argument("--fast", action="store_true",
                    help="実時間を待たずに最速で送る（連結の挙動が本番と変わる）")
    ap.add_argument("--timeout", type=float, default=120.0, help="停止応答の待ち時間（秒）")
    ap.add_argument("--out", help="本文の書き出し先")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
