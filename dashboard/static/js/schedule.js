document.getElementById("add-schedule").onclick = async (e) => {
  const [hour, minute] = document.getElementById("sch-time").value.split(":").map(Number);
  await withLoading(e.currentTarget, async () => {
    try {
      await apiFetch("/api/schedules", { method: "POST", body: JSON.stringify({
        action: document.getElementById("sch-action").value,
        hour, minute,
        days_of_week: document.getElementById("sch-days").value,
        source_id: document.getElementById("sch-source").value || null,
      })});
      toast("Schedule added"); location.reload();
    } catch (err) { toast(err.message, "err"); }
  });
};
document.querySelectorAll(".delete-schedule").forEach(btn => btn.addEventListener("click", async () => {
  if (!(await confirmDialog("Delete this schedule?", { danger: true }))) return;
  try { await apiFetch(`/api/schedules/${btn.dataset.id}`, { method: "DELETE" }); location.reload(); }
  catch (err) { toast(err.message, "err"); }
}));
