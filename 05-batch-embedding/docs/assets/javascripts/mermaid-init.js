mermaid.initialize({
  startOnLoad: false,
  theme: "neutral",
  securityLevel: "strict",
});

const renderMermaid = async () => {
  document.querySelectorAll("pre.mermaid").forEach((wrapper) => {
    const diagram = document.createElement("div");
    diagram.className = "mermaid";
    diagram.textContent = wrapper.textContent;
    wrapper.replaceWith(diagram);
  });
  await mermaid.run({ querySelector: ".mermaid" });
  window.dispatchEvent(new CustomEvent("mermaid:rendered"));
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", renderMermaid, { once: true });
} else {
  renderMermaid();
}
