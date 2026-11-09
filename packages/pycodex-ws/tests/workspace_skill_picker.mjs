import assert from "node:assert/strict";
import {skillQuery, matchingSkills, SkillPicker} from "../workspace_server/frontend/skill_picker.mjs";
import {Composer} from "../workspace_server/frontend/composer.mjs";

const draft = text => ({parts: [{type: "text", text}], selection: {start: text.length, end: text.length}});
const query = text => { const value = draft(text); return skillQuery(value.parts, value.selection); };
for (const marker of ["@", "$"]) {
  assert.equal(query(marker).prefix, "");
  assert.equal(query(`use ${marker}work`).prefix, "work");
}
for (const text of ["email@example", "foo$work", "${HOME}", "$(pwd)", "@work ", "$HOME/"]) {
  assert.equal(query(text), null);
}
assert.equal(query("中文 😀 $wor").start, "中文 😀 ".length);
assert.equal(skillQuery([{type: "context", text: "huge context"},
  {type: "text", text: " $wor"}], {start: 6, end: 6}).start, 2);
assert.equal(skillQuery(draft("$work-context").parts, {start: 3, end: 3}), null);
assert.equal(skillQuery(draft("$wor").parts, {start: 0, end: 4}), null);

const skills = [
  {name: "work-context", description: "Personal notes"},
  {name: "workflow", description: "<unsafe>"},
];
assert.deepEqual(matchingSkills(skills, "WOR"), skills);

class Element {
  constructor() { this.children = []; this.attributes = {}; this.handlers = {}; this.id = "prompt"; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, value) { this.handlers[key] = value; }
  scrollIntoView() {}
}
globalThis.document = {createElement: () => new Element()};
const composer = Object.create(Composer.prototype);
composer.element = new Element();
composer.element.parentElement = new Element();
document.activeElement = composer.element;
let value = draft("@wor"), submitted = 0, errors = [];
composer.read = () => value;
composer.submit = () => { submitted++; };
composer.error = message => errors.push(message);
composer.replace = (start, end, inserted) => {
  const text = Composer.text(value.parts);
  value = draft(text.slice(0, start) + inserted[0].text + text.slice(end));
};
let loads = 0, resolve;
const picker = new SkillPicker(composer, () => {
  loads++;
  return new Promise(done => { resolve = done; });
});
composer.skillPicker = picker;
picker.update();
await Promise.resolve(); // Start the lazy request.
value = draft("$work"); // A response must use the current draft, not the original prefix.
resolve(skills);
await picker.loading;
assert.equal(loads, 1);
assert.equal(picker.menu.hidden, false);
assert.equal(picker.menu.children[1].children[1].textContent, "<unsafe>");
function press(key, options = {}) {
  let prevented = false;
  composer.keydown({key, ...options, preventDefault: () => { prevented = true; }});
  return prevented;
}
assert.equal(press("ArrowDown"), true);
assert.equal(picker.index, 1);
assert.equal(press("ArrowUp"), true);
assert.equal(picker.index, 0);
assert.equal(press("Enter", {isComposing: true}), false);
assert.equal(submitted, 0);
assert.equal(press("Enter"), true);
assert.equal(Composer.text(value.parts), "$work-context ");
assert.equal(submitted, 0);
assert.equal(press("Enter"), true);
assert.equal(submitted, 1);
value = draft("@wor");
picker.update();
assert.equal(press("Escape"), true);
picker.update();
assert.equal(picker.menu.hidden, true);
value = draft("@work");
picker.update();
assert.equal(picker.menu.hidden, false);
assert.equal(press("Tab"), true);
assert.equal(Composer.text(value.parts), "$work-context ");
value = draft("$wor");
picker.update();
document.activeElement = null;
picker.update();
assert.equal(picker.menu.hidden, true);
assert.deepEqual(errors, []);
console.log("Skill picker checks passed.");
