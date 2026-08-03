(() => {
  const closeLightbox = (lightbox) => {
    lightbox.hidden = true;
    lightbox.querySelector(".diagram-lightbox__content").replaceChildren();
    document.body.classList.remove("diagram-lightbox-open");
  };

  const lightbox = document.createElement("div");
  lightbox.className = "diagram-lightbox";
  lightbox.hidden = true;
  lightbox.setAttribute("role", "dialog");
  lightbox.setAttribute("aria-modal", "true");
  lightbox.setAttribute("aria-label", "Enlarged architecture diagram");

  const content = document.createElement("div");
  content.className = "diagram-lightbox__content";
  lightbox.append(content);
  document.body.append(lightbox);

  const openLightbox = (media) => {
    const source = media.matches("img") ? media : media.querySelector("svg");
    if (!source) return;
    content.replaceChildren(source.cloneNode(true));
    lightbox.hidden = false;
    document.body.classList.add("diagram-lightbox-open");
    lightbox.focus();
  };

  lightbox.tabIndex = -1;
  lightbox.addEventListener("click", (event) => {
    if (event.target === lightbox) closeLightbox(lightbox);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !lightbox.hidden) closeLightbox(lightbox);
  });

  const initializeZoomableMedia = () => {
    document.querySelectorAll(".mermaid, .zoomable-media").forEach((media) => {
      if (media.dataset.zoomInitialized) return;
      media.dataset.zoomInitialized = "true";
      media.tabIndex = 0;
      media.setAttribute("role", "button");
      media.setAttribute("aria-label", "Enlarge image");
      media.title = "Enlarge image";
      media.addEventListener("click", () => openLightbox(media));
      media.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openLightbox(media);
        }
      });
    });
  };

  initializeZoomableMedia();
  window.addEventListener("mermaid:rendered", initializeZoomableMedia);
})();
