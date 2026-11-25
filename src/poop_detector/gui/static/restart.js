// 再起動前の応答を成功と数えないよう、起動ごとの識別子と対象を確かめる。
(() => {
  const form = document.getElementById("restart-form");
  const message = document.getElementById("restart-message");
  const button = form.querySelector("button");
  const generation = form.elements.namedItem("generation");
  const endpoint = form.action;

  async function waitForRestart() {
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      try {
        const response = await fetch(endpoint, {
          cache: "no-store",
          signal: AbortSignal.timeout(3000),
        });
        if (!response.ok) continue;
        const status = await response.json();
        if (status.command === generation.dataset.command && status.ready
            && status.generation !== generation.value) {
          message.textContent = "再起動が完了しました。最新の稼働状況は画面を更新して確認できます。";
          return;
        }
      } catch {
        // 停止中は接続できないため、次の応答を待つ。
      }
    }
    message.textContent = "再起動後の復帰を確認できませんでした。起動時のログと設定を確認し、必要なら元のコマンドで起動し直してください。";
  }

  if (button.disabled) waitForRestart();
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    message.textContent = "再起動を要求しています。";
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        signal: AbortSignal.timeout(5000),
      });
      if (response.status !== 202) {
        message.textContent = response.status === 409
          ? "すでに処理中か、画面が古くなっています。画面を更新して確認してください。"
          : "再起動の要求が拒否されました。画面を更新してやり直してください。";
        return;
      }
      message.textContent = "再起動中です。復帰を待っています。";
    } catch {
      message.textContent = "応答が途切れました。再起動後の復帰を待っています。";
    }
    await waitForRestart();
  });
})();
