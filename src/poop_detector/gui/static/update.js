// 現行サービスが準備中でも状態を確認し、切り替え先の版と起動を確かめる。
(() => {
  const form = document.getElementById("update-form");
  const message = document.getElementById("update-message");
  const button = form.querySelector("button");
  let status;

  function showStatus(value) {
    status = value;
    form.hidden = !value.enabled || !value.available;
    button.disabled = false;
    if (!value.latest) {
      message.textContent = "PyPI の安定版を取得できませんでした。画面を更新して再確認してください。";
    } else if (!value.available) {
      message.textContent = `PyPI の最新安定版は ${value.latest} です。`;
    } else {
      const hint = value.enabled
        ? "更新先の準備ができると、自動で再起動します。"
        : "この起動方法では、利用中のインストール手順で更新してください。";
      message.textContent = `新しい安定版 ${value.latest} が利用できます。${hint}`;
    }
  }

  async function check() {
    const response = await fetch("/manage/update/status", {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) throw new Error();
    return await response.json();
  }

  async function waitForUpdate(target, generation) {
    button.disabled = true;
    const deadline = Date.now() + 600000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      try {
        const newer = await check();
        if (newer.ready && newer.current === target && newer.generation !== generation) {
          form.hidden = true;
          message.textContent = `${target} に更新しました。画面を更新してください。`;
          return;
        }
        if (newer.phase === "failed") {
          showStatus(newer);
          message.textContent = "更新に失敗しました。現在の版で稼働しています。ログを確認してから再試行してください。";
          return;
        }
        if (newer.phase === "preparing") {
          message.textContent = `${target} の配布物を取得し、別の環境で起動を確認しています。`;
        }
      } catch {
        message.textContent = `${target} の起動を待っています。`;
      }
    }
    message.textContent = "更新後の復帰を確認できません。起動ログを確認してください。必要なら更新ディレクトリの active を戻し、元の起動コマンドで再起動してください。";
  }

  check().then(value => {
    showStatus(value);
    if (value.phase === "preparing" || value.phase === "restarting") {
      waitForUpdate(value.target, value.generation);
    } else if (value.phase === "failed") {
      message.textContent = "前回の更新に失敗しました。起動ログを確認してから再試行してください。";
    }
  }).catch(() => {
    message.textContent = "バージョン情報を取得できませんでした。";
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!status?.available || button.disabled) return;
    button.disabled = true;
    const { latest, generation } = status;
    const body = new URLSearchParams(new FormData(form));
    body.set("target", latest);
    body.set("generation", generation);
    message.textContent = `${latest} への更新を準備しています。`;
    try {
      const response = await fetch(form.action, {
        method: "POST", body, signal: AbortSignal.timeout(10000),
      });
      if (response.status !== 202) {
        message.textContent = "更新を開始できませんでした。画面を更新して確認してください。";
        return;
      }
    } catch {
      // 書き込み後に接続が途切れた場合も、更新が受理されたかを確認する。
    }
    await waitForUpdate(latest, generation);
  });
})();
