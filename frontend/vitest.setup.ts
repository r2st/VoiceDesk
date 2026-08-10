import "@testing-library/jest-dom/vitest";

// Node 22+ defines a global `localStorage` accessor that throws/returns
// undefined without a `--localstorage-file` flag, and it shadows jsdom's own
// implementation since jsdom's window global is the same object as
// `globalThis` here. Replace it with a plain in-memory Storage so
// `window.localStorage` behaves the way it does in a real browser.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }

  clear(): void {
    this.store.clear();
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

const memoryStorage = new MemoryStorage();
for (const target of [globalThis, window] as const) {
  Object.defineProperty(target, "localStorage", {
    value: memoryStorage,
    configurable: true,
    writable: true,
  });
}
