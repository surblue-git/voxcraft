// 画面の自動消灯を抑える（Screen Wake Lock API）。
//
// Android の Obsidian（WebView）では画面が消えると AudioContext ごと止まり、
// 録音が途切れる。長時間まわす文字起こしでは致命的なので、その間だけ画面を
// 起こしておく。抑えられるのは「放置による自動消灯」だけで、電源ボタンや
// アプリ切り替えは OS 側が解放する（そこは止まる）。
//
// wake lock は文書が hidden になると自動解放される仕様なので、表示に戻ったら
// 取り直す。非対応・拒否の環境では黙って効かなくなるため、acquire() の戻り値で
// 呼び出し側に知らせて通知させる。

interface WakeLockSentinelLike {
    released: boolean;
    release(): Promise<void>;
    addEventListener(type: "release", cb: () => void): void;
}

type WakeLockNavigator = Navigator & {
    wakeLock?: { request(type: "screen"): Promise<WakeLockSentinelLike> };
};

export class ScreenWakeLock {
    private sentinel: WakeLockSentinelLike | null = null;
    private wanted = false;
    private listening = false;
    private onVisibility = (): void => {
        void this.reacquire();
    };

    get held(): boolean {
        return this.sentinel !== null && !this.sentinel.released;
    }

    // 取得できたら true。非対応・拒否なら false。
    async acquire(): Promise<boolean> {
        this.wanted = true;
        if (!this.listening) {
            document.addEventListener("visibilitychange", this.onVisibility);
            this.listening = true;
        }
        return this.request();
    }

    async release(): Promise<void> {
        this.wanted = false;
        if (this.listening) {
            document.removeEventListener("visibilitychange", this.onVisibility);
            this.listening = false;
        }
        const s = this.sentinel;
        this.sentinel = null;
        if (s && !s.released) {
            try {
                await s.release();
            } catch {
                // 既に OS 側で解放済み。
            }
        }
    }

    private async request(): Promise<boolean> {
        if (this.held) return true;
        const api = (navigator as WakeLockNavigator).wakeLock;
        if (!api) return false;
        try {
            const s = await api.request("screen");
            // 自分で release() する前に OS 側が解放することがある（電源ボタン等）。
            s.addEventListener("release", () => {
                if (this.sentinel === s) this.sentinel = null;
            });
            this.sentinel = s;
            return true;
        } catch {
            return false;
        }
    }

    // 表示に戻ったときの取り直し。hidden の間の request() は必ず失敗するので試さない。
    private async reacquire(): Promise<void> {
        if (!this.wanted || document.visibilityState !== "visible") return;
        await this.request();
    }
}
