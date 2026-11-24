(function () {
  const dialog = document.getElementById("reuse-evidence");
  if (!dialog) return;
  const form = dialog.querySelector("form");
  const feedback = document.getElementById("reuse-feedback");
  const question = form.elements.question, newTitle = form.elements.title;
  const error = form.querySelector("[role=alert]");
  let entry, saving = false;
  function displayTitle() {
    newTitle.parentElement.hidden = !!question.value;
    newTitle.required = !question.value;
  }
  document.querySelectorAll("[data-reuse-entry]").forEach(button => {
    button.addEventListener("click", () => {
      entry = button.dataset.reuseEntry;
      form.reset(); displayTitle(); error.hidden = true;
      dialog.showModal();
      question.focus();
    });
  });
  question.addEventListener("change", displayTitle);
  form.querySelector("[data-close]").addEventListener("click", () => { if (!saving) dialog.close(); });
  dialog.addEventListener("cancel", e => { if (saving) e.preventDefault(); });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    if (saving) return;
    saving = true;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true; error.hidden = true;
    try {
      const result = await pmApi("/api/evidence/entries/" + entry + "/reuse", {
        question_id: Number(question.value), question_title: newTitle.value, stance: form.elements.stance.value,
        dimension: form.elements.dimension.value, note: form.elements.note.value
      });
      if (!result.ok) throw Error(result.message || t("操作未完成，请重试。"));
      const target = question.value ? question.selectedOptions[0].text : newTitle.value.trim();
      if (!Array.from(question.options).some(o => o.value === String(result.question_id))) {
        question.add(new Option(target, result.question_id), question.lastElementChild);
      }
      feedback.querySelector("span").textContent = result.created ? t("摘录已关联到") + " " + target : t("这个问题已收录这条摘录，原笔记已保留。");
      feedback.querySelector("a").href = "/evidence?question=" + result.question_id + "#evidence-" + result.id;
      feedback.hidden = false;
      const trigger = document.querySelector('[data-reuse-entry="' + entry + '"]');
      trigger?.after(feedback);
      dialog.close();
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { saving = false; button.disabled = false; }
  });

  const area = document.querySelector("[data-overlaps]");
  if (!area) return;
  for (const group of area.querySelectorAll("[data-dataset]")) {
    const reviews = JSON.parse(group.dataset.reviews), pair = group.querySelector("form"), fields = pair.elements;
    const button = pair.querySelector("button"), status = pair.querySelector("[role=status]");
    function ordered() {
      return [fields.paper_a.selectedOptions[0], fields.paper_b.selectedOptions[0]].sort((a,b) => Number(a.value)-Number(b.value));
    }
    function showReviewedPairs() {
      const list = group.querySelector("[data-reviewed-pairs]");
      const papers = new Map(Array.from(fields.paper_a.options, o => [Number(o.value), o.text]));
      const labels = new Map(Array.from(fields.decision.options, o => [o.value, o.text]));
      list.replaceChildren();
      list.hidden = !reviews.length;
      for (const review of reviews) {
        const item = document.createElement("li"), link = document.createElement("button");
        link.type = "button"; link.className = "ghost small";
        link.textContent = papers.get(review.paper_a) + " / " + papers.get(review.paper_b) + " · " +
          labels.get(review.decision) + (review.stale ? " · " + t("待核对") : "");
        link.addEventListener("click", () => {
          fields.paper_a.value = review.paper_a; fields.paper_b.value = review.paper_b;
          showSaved(); fields.decision.focus();
        });
        item.append(link); list.append(item);
      }
    }
    function showSaved() {
      const [a,b] = ordered();
      const review = reviews.find(r => r.paper_a === Number(a.value) && r.paper_b === Number(b.value));
      fields.decision.value = review?.decision || "";
      fields.note.value = review?.note || "";
      button.disabled = a.value === b.value;
      status.textContent = button.disabled ? t("请选择两篇不同的论文。") : !review ? "" :
        review.stale ? t("原文上下文已更新，请重新核对。") : t("已保存核对结论");
    }
    fields.paper_a.addEventListener("change", showSaved); fields.paper_b.addEventListener("change", showSaved);
    showReviewedPairs(); showSaved();
    pair.addEventListener("submit", async e => {
      e.preventDefault();
      if (button.disabled) return;
      const [a,b] = ordered();
      const params = {kind:group.dataset.kind, accession:group.dataset.dataset, paper_a:Number(a.value), paper_b:Number(b.value),
                      context_key:a.dataset.key + b.dataset.key, decision:fields.decision.value, note:fields.note.value.trim()};
      const prior = reviews.find(r => r.paper_a === params.paper_a && r.paper_b === params.paper_b);
      params.previous = prior ? {decision:prior.decision, note:prior.note, context_key:prior.context_key} : null;
      for (const el of Array.from(fields)) el.disabled = true;
      try {
        const result = await pmApi("/api/evidence/questions/" + area.dataset.overlaps + "/dataset-overlap", params);
        if (!result.ok) throw Error(result.message || t("操作未完成，请重试。"));
        if (prior) Object.assign(prior, params, {stale:false}); else reviews.push({...params, stale:false});
        showReviewedPairs();
        status.textContent = t("已保存核对结论");
      } catch (err) { status.textContent = err.message; }
      finally { for (const el of Array.from(fields)) el.disabled = false; }
    });
  }
})();
