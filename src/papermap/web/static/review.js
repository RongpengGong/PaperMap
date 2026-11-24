(() => {
  for (const form of document.querySelectorAll("[data-pf-form]")) {
    const card = form.closest(".review-item");
    const submit = form.querySelector(".pf-confirm");
    const status = form.querySelector("[data-build-status]");
    const url = "/api/review/pending/" + form.elements.pending_id.value + "/establishment";
    let working = false;

    function setWorking(value, submitting = false) {
      working = value;
      form.setAttribute("aria-busy", String(value));
      for (const control of card.querySelectorAll("button, select, input:not([type=hidden])")) control.disabled = value;
      submit.textContent = value ? t("正在建立…") : "✦ " + t("确认建立");
      if (value) {
        status.classList.remove("err-text");
        status.textContent = submitting ? t("正在提交…") : t("已提交，正在等待或写入分类表。你可以继续浏览。");
      }
    }

    function fail(message) {
      setWorking(false);
      status.classList.add("err-text");
      status.textContent = message;
    }

    async function poll() {
      try {
        const response = await fetch(url, {cache: "no-store"});
        if (!response.ok) throw Error();
        const result = await response.json();
        if (result.state === "done") {
          location.assign(result.url);
        } else if (result.state === "working") {
          setTimeout(poll, 650);
        } else {
          fail(result.message || t("任务已中断，可以重新确认建立。"));
        }
      } catch {
        fail(t("暂时无法读取建立结果，请刷新本页查看。"));
      }
    }

    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (working) return;
      const data = new FormData(form);
      data.set("action", "establish");
      setWorking(true, true);
      try {
        const response = await fetch(form.getAttribute("action"), {method: "POST", body: data, headers: {Accept: "application/json"}});
        if (response.status === 403) {
          fail(t("页面已过期，请刷新后重试。"));
          return;
        }
        if (!response.ok) throw Error();
        setWorking(true);
        await poll();
      } catch {
        fail(t("提交结果未知，请刷新本页查看。"));
      }
    });

    if (form.dataset.buildState === "working") {
      setWorking(true);
      poll();
    }
  }
})();
