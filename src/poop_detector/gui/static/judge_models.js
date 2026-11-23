(() => {
  const form = document.getElementById("judge-model-form");
  const { provider, model, custom_model: custom, csrf } = form.elements;
  const customField = document.getElementById("judge-custom-model");
  const status = document.getElementById("judge-model-status");
  const [defaultOption, customOption] = model.options;
  const initialProvider = provider.value;
  const initialModel = model.value;
  let pending;

  function showCustom() {
    const enabled = model.value === customOption.value;
    customField.hidden = !enabled;
    custom.disabled = !enabled;
  }

  async function loadModels() {
    pending?.abort();
    const request = new AbortController();
    pending = request;
    status.textContent = "モデル候補を取得しています…";
    try {
      const response = await fetch(form.dataset.catalog, {
        method: "POST",
        body: new URLSearchParams({ provider: provider.value, csrf: csrf.value }),
        signal: request.signal,
      });
      if (!response.ok) throw new Error();
      const { models } = await response.json();
      if (request.signal.aborted) return;

      const selected = model.value;
      const options = models.map(name => new Option(name, name));
      model.replaceChildren(defaultOption, ...options, customOption);
      model.value = selected === customOption.value && custom.value === custom.defaultValue
        && models.includes(custom.value) ? custom.value : selected;
      showCustom();
      status.textContent = "";
    } catch {
      if (request.signal.aborted) return;
      status.textContent = "モデル一覧を取得できませんでした。既定かモデル名の入力を選べます。";
    }
  }

  provider.addEventListener("change", () => {
    model.replaceChildren(defaultOption, customOption);
    const initial = provider.value === initialProvider;
    model.value = initial ? initialModel : "";
    custom.value = initial ? custom.defaultValue : "";
    showCustom();
    void loadModels();
  });
  model.addEventListener("change", showCustom);
  showCustom();
  void loadModels();
})();
