/** Whitespace-delimited skill mentions; offsets count context chips as one character. */
export function skillQuery(parts, selection) {
  if (selection.start !== selection.end) return null;
  const text = parts.map(part => part.type === "text" ? part.text : "\uFFFC").join("");
  const prefix = text.slice(0, selection.start);
  const match = /(?:^|\s)([@$][A-Za-z0-9_-]*)$/.exec(prefix);
  if (!match || /^[A-Za-z0-9_-]/.test(text.slice(selection.end))) return null;
  return {start: selection.start - match[1].length, end: selection.end,
    prefix: match[1].slice(1), key: `${selection.start}:${match[1]}`};
}

export function matchingSkills(skills, prefix) {
  return skills.filter(skill => skill.name.toLowerCase().startsWith(prefix.toLowerCase()));
}

export class SkillPicker {
  constructor(composer, loadSkills) {
    this.composer = composer;
    this.loadSkills = loadSkills;
    this.skills = [];
    this.loadedAt = 0;
    this.loading = null;
    this.index = 0;
    this.query = null;
    this.dismissed = null;
    this.menu = document.createElement("div");
    this.menu.className = "skill-picker";
    this.menu.id = `${composer.element.id || "composer"}-skills`;
    this.menu.setAttribute("role", "listbox");
    this.menu.setAttribute("aria-label", "Skills");
    this.menu.hidden = true;
    composer.element.parentElement.append(this.menu);
    composer.element.setAttribute("aria-controls", this.menu.id);
    composer.element.setAttribute("aria-autocomplete", "list");
    composer.element.addEventListener("blur", () => this.hide());
    composer.element.addEventListener("focus", () => this.update());
  }

  hide() {
    this.menu.hidden = true;
    this.composer.element.setAttribute("aria-expanded", "false");
    this.composer.element.removeAttribute("aria-activedescendant");
  }

  refresh() {
    if (this.loading || Date.now() - this.loadedAt < 15000) return;
    this.loading = Promise.resolve().then(() => this.loadSkills()).then(skills => {
      this.skills = skills;
      this.loadedAt = Date.now();
    }).catch(error => {
      this.loadedAt = Date.now(); // Avoid a request/error loop on each cursor movement.
      this.skills = [];
      this.composer.error(`Cannot load skills: ${error.message}`);
    }).finally(() => {
      this.loading = null;
      this.update();
    });
  }

  update() {
    const {composer} = this;
    if (document.activeElement !== composer.element || composer.composing) {
      this.hide();
      return;
    }
    const current = composer.read();
    const query = skillQuery(current.parts, current.selection);
    if (!query) {
      this.dismissed = null;
      this.query = null;
      this.hide();
      return;
    }
    if (query.key === this.dismissed) { this.hide(); return; }
    if (this.query?.key !== query.key) this.index = 0;
    this.query = query;
    this.refresh();
    this.matches = matchingSkills(this.skills, query.prefix);
    if (!this.matches.length) { this.hide(); return; }
    this.index = Math.min(this.index, this.matches.length - 1);
    this.render();
  }

  render() {
    this.menu.replaceChildren();
    this.matches.forEach((skill, index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.tabIndex = -1;
      option.id = `${this.menu.id}-${index}`;
      option.className = "skill-option";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(index === this.index));
      const name = document.createElement("strong");
      name.textContent = `$${skill.name}`;
      const description = document.createElement("span");
      description.textContent = skill.description;
      option.append(name, description);
      option.title = skill.description;
      option.addEventListener("mousedown", event => event.preventDefault());
      option.addEventListener("click", () => this.accept(index));
      this.menu.append(option);
    });
    this.menu.hidden = false;
    this.composer.element.setAttribute("aria-expanded", "true");
    this.composer.element.setAttribute("aria-activedescendant", `${this.menu.id}-${this.index}`);
    this.menu.children[this.index].scrollIntoView({block: "nearest"});
  }

  accept(index = this.index) {
    // Recheck the range so a stale response or click cannot overwrite a new draft.
    const current = this.composer.read();
    const query = skillQuery(current.parts, current.selection);
    if (!query || query.key !== this.query?.key) { this.hide(); return; }
    const skill = this.matches[index];
    this.hide();
    this.composer.replace(query.start, query.end, [{type: "text", text: `$${skill.name} `}]);
  }

  keydown(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return false;
    this.update();
    if (this.menu.hidden) return false;
    if (!["ArrowUp", "ArrowDown", "Enter", "Tab", "Escape"].includes(event.key)) return false;
    event.preventDefault();
    if (event.key === "Escape") {
      this.dismissed = this.query.key;
      this.hide();
    } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      this.index = (this.index + (event.key === "ArrowDown" ? 1 : -1) + this.matches.length) % this.matches.length;
      this.render();
    } else this.accept();
    return true;
  }
}
