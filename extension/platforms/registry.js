(() => {
  "use strict";

  // Platform profiles are the ONLY place that may contain site-specific DOM
  // knowledge. Everything under dom/ and actions/ stays generic so a new site
  // (Facebook, Instagram, Zalo Web) only needs one more profile file.
  const profiles = [];

  function register(profile) {
    if (!profile || !profile.id || typeof profile.matches !== "function") {
      throw new Error("A platform profile needs an id and a matches() function");
    }
    profiles.push(Object.freeze(profile));
  }

  function match(loc = location) {
    return profiles.find((profile) => {
      try {
        return profile.matches(loc);
      } catch (_) {
        return false;
      }
    }) || null;
  }

  globalThis.AgentPlatforms = Object.freeze({ register, match, all: () => profiles.slice() });
})();
