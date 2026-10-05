import { readFileSync } from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.attrs = {}; this.style = {}; this.value = ''; this.hidden = false; this.className = ''; this._text = ''; }
  append(...children) { children.forEach(c => { if (typeof c === 'object') c.parentNode = this; }); this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  get tHead() { return this.children.find(c => c.tagName === 'thead'); }
  get tBodies() { return this.children.filter(c => c.tagName === 'tbody'); }
  querySelectorAll(selector) { return this.children.flatMap(c => typeof c === 'object' ? [...(selector.startsWith('.') && c.className.split(' ').includes(selector.slice(1)) ? [c] : []), ...c.querySelectorAll(selector)] : []); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); }
  replaceWith(child) { if (this.parentNode) this.parentNode.children = this.parentNode.children.map(c => c === this ? child : c); }
  after(child) { if (this.parentNode) { const index = this.parentNode.children.indexOf(this); this.parentNode.children.splice(index + 1, 0, child); child.parentNode = this.parentNode; } }
  replaceChildren(...children) { this.children = []; this.append(...children); this._text = ''; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name]; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener() {}
  click() { this.onclick?.(); }
  get textContent() { return this._text + this.children.map(c => typeof c === 'object' ? c.textContent : String(c)).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
}
export const source = file => readFileSync(new URL(`../../static/js/${file}`, import.meta.url), 'utf8');
export const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
export function browser(fetch) {
  const elements = new Map(), downloads = [], intervals = [];
  const context = vm.createContext({
    fetch, URL, URLSearchParams, Headers, AbortController, console,
    location: { href: 'http://localhost/', origin: 'http://localhost', hash: '' },
    matchMedia: () => ({ matches: true }), addEventListener() {}, setTimeout: () => 1, clearTimeout() {}, setInterval(fn) { intervals.push(fn); },
    document: { querySelector: selector => { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
      querySelectorAll: () => [], createElement: tag => { const e = new Element(tag); if (tag === 'a') e.click = () => downloads.push(e.href); return e; },
      createElementNS: (ns, tag) => new Element(tag), addEventListener() {} },
    scrollTo() {}, scrollX: 0, scrollY: 0, innerWidth: 1000,
  });
  const run = script => vm.runInContext(script, context);
  run(source('app.js')); run(source('mock.js'));
  return { run, context, get: selector => elements.get(selector), downloads, intervals };
}
export const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body, headers: new Headers() });
