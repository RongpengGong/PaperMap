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

// ---------------------------------------------------------------- batch decisions
(function () {
  const api = (url, body) => window.pmApi(url, body);
  document.addEventListener("click", async e => {
    const one = e.target.closest("[data-dismiss-translation]");
    if (one) {
      const j = await api("/api/review/translations/dismiss", { paper_ids: [one.dataset.dismissTranslation] });
      if (j.ok) one.closest("li").remove();
      return;
    }
    if (e.target.closest("[data-dismiss-all-translations]")) {
      if (!(await window.pmDialog(t("把列表里所有可能漏译的译文都标为「不用补」？译文或 PDF 变了会重新出现。")))) return;
      if ((await api("/api/review/translations/dismiss", { all: true })).ok) location.reload();
      return;
    }
    const all = e.target.closest("[data-patch-all]");
    if (all) {
      const btns = [...document.querySelectorAll("[data-patch-translation]:not(:disabled)")];
      if (!btns.length) return;
      if (!(await window.pmDialog(`${t("给")} ${btns.length} ${t("篇论文补译？只翻译漏掉的段落，任务在后台逐篇进行。")}`, { okText: t("全部补译") }))) return;
      all.disabled = true;
      let ack = false, done = 0;
      for (const b of btns) {
        const pid = b.dataset.patchTranslation;
        let j = await api(`/paper/${pid}/translate`, { service: "", reuse: true, ack });
        if (j.need_ack) {
          if (!(await window.pmDialog(t("使用 bilibili 免费翻译时，论文正文会发送到 B 站的服务器（index-translate.bilibili.com）。确定继续？以后不再询问。"), { okText: t("继续翻译") }))) break;
          ack = true;
          j = await api(`/paper/${pid}/translate`, { service: "", reuse: true, ack });
        }
        if (!j.ok) { window.pmToast(j.message || t("操作失败")); break; }
        b.disabled = true; b.textContent = t("补译中"); b.nextElementSibling?.remove(); done++;
      }
      window.pmToast(`${t("已排队补译")} ${done} ${t("篇")}`);
      return;
    }
    const lb = e.target.closest("[data-labels]");
    if (lb) {
      const action = lb.dataset.labels, min = Number(lb.dataset.min || 0);
      const msg = action === "reject" ? t("拒绝所有待确认的分类标签？")
        : (min ? t("接受置信度不低于 45% 的建议标签？更低的留在这里。") : t("接受所有待确认的分类标签？"));
      if (!(await window.pmDialog(msg))) return;
      lb.disabled = true;
      const j = await api("/api/review/labels", { action, min_confidence: min });
      if (j.ok) location.reload(); else { lb.disabled = false; window.pmToast(j.message || t("操作失败")); }
    }
  });
})();
