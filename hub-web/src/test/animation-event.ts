// jsdom has no AnimationEvent, so react-dom (which probes for it when it loads)
// listens for `webkitAnimationEnd` and never sees Testing Library's
// `animationend`. Define it before react-dom is imported: test-setup.ts imports
// this module first.
if (!("AnimationEvent" in window)) {
  Object.defineProperty(window, "AnimationEvent", { value: class AnimationEvent extends Event {} });
}
