<script lang="ts">
  import {
    createHotkey,
    createHotkeySequence,
    type Hotkey,
  } from "@tanstack/svelte-hotkeys";
  import {
    formatKeys,
    type Keys,
    type Shortcut,
    type ShortcutGroup,
  } from "../lib/shortcuts.ts";
  import Icon from "./Icon.svelte";

  /**
   * Binds a page's shortcuts and draws the `?` help panel from the same
   * list. Mount it once per page, last in the markup so the hint pill
   * sits over everything.
   *
   * Escape is bound here rather than by the pages: the help panel goes
   * first, then leaving a focused field, then whatever the page listed
   * under Escape (closing an inspector, say). Everything else is
   * disabled while the panel is open, so j and k do not scroll the
   * table behind it.
   */
  const { groups, page }: { groups: ShortcutGroup[]; page: string } = $props();

  let open = $state(false);
  let dialog = $state<HTMLDialogElement | null>(null);

  const whenClosed = () => ({ enabled: !open });

  // `Keys` is plain string so lib/shortcuts.ts stays free of the library;
  // TanStack validates the spelling when it registers.
  function bind(keys: Keys, run: () => void) {
    if (Array.isArray(keys)) {
      createHotkeySequence(keys as Hotkey[], run, whenClosed);
    } else {
      createHotkey(keys as Hotkey, run, whenClosed);
    }
  }

  const escapeActions: Shortcut[] = [];
  // The groups are read once: a page's shortcuts are fixed for its life,
  // and TanStack registers each binding in its own $effect.
  // svelte-ignore state_referenced_locally
  for (const group of groups) {
    for (const shortcut of group.shortcuts) {
      if (shortcut.keys === "Escape") {
        escapeActions.push(shortcut);
        continue;
      }
      bind(shortcut.keys, shortcut.run);
      for (const alt of shortcut.also ?? []) bind(alt, shortcut.run);
    }
  }

  // `?` is not a key name TanStack accepts: Shift plus the slash key is.
  createHotkey("Shift+/" as Hotkey, () => {
    open = !open;
  });

  function isField(el: Element): boolean {
    return (
      el instanceof HTMLInputElement ||
      el instanceof HTMLSelectElement ||
      el instanceof HTMLTextAreaElement ||
      (el instanceof HTMLElement && el.isContentEditable)
    );
  }

  createHotkey(
    "Escape",
    () => {
      if (open) {
        open = false;
        return;
      }
      const active = document.activeElement;
      if (active instanceof HTMLElement && isField(active)) {
        active.blur();
        return;
      }
      for (const shortcut of escapeActions) shortcut.run();
    },
    { preventDefault: false },
  );

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  });

  /** kbd labels for one help row: the override, or the keys, with
   * "then" between the steps of a sequence. */
  function steps(s: Shortcut): { label: string; then: boolean }[] {
    const labels = s.display ?? formatKeys(s.keys);
    return labels.map((label, i) => ({
      label,
      then: i > 0 && s.display === undefined,
    }));
  }
</script>

<dialog
  bind:this={dialog}
  class="shortcuts"
  aria-labelledby="shortcuts-title"
  onclose={() => (open = false)}
  onclick={(e) => {
    if (e.target === dialog) open = false;
  }}
>
  <div class="panel">
    <header>
      <h2 id="shortcuts-title"><Icon name="keyboard" /> Keyboard shortcuts</h2>
      <span class="page">{page}</span>
      <button
        type="button"
        class="close"
        aria-label="Close"
        onclick={() => (open = false)}><Icon name="close" /></button
      >
    </header>

    <div class="groups">
      {#each groups as group (group.title)}
        {@const listed = group.shortcuts.filter((s) => !s.hidden)}
        {#if listed.length > 0}
          <section>
            <h3>{group.title}</h3>
            <dl>
              {#each listed as s (s.label)}
                <dt>
                  {#each steps(s) as step, i (i)}
                    {#if step.then}<span class="then">then</span>{/if}<kbd
                      >{step.label}</kbd
                    >
                  {/each}
                  {#each s.also ?? [] as alt (alt)}
                    <span class="then">or</span><kbd>{formatKeys(alt)[0]}</kbd>
                  {/each}
                </dt>
                <dd>{s.label}</dd>
              {/each}
            </dl>
          </section>
        {/if}
      {/each}
      <section>
        <h3>This panel</h3>
        <dl>
          <dt><kbd>?</kbd></dt>
          <dd>Show or hide it</dd>
          <dt><kbd>Esc</kbd></dt>
          <dd>Close it</dd>
        </dl>
      </section>
    </div>

    <footer>
      Shortcuts pause while a text field or menu has focus; <kbd>Esc</kbd>
      leaves it. A pair like <kbd>g</kbd> <span class="then">then</span>
      <kbd>t</kbd> is two keys within a second. Datasette's own
      <kbd>/</kbd> still opens its search.
    </footer>
  </div>
</dialog>

<button
  type="button"
  class="hint"
  title="Keyboard shortcuts (press ?)"
  onclick={() => (open = true)}
>
  <Icon name="keyboard" />
  <kbd>?</kbd>
</button>

<style>
  dialog.shortcuts {
    width: min(880px, calc(100vw - 2rem));
    max-height: calc(100vh - 2rem);
    padding: 0;
    border: 1px solid #d7dee6;
    border-radius: 8px;
    box-shadow: 0 12px 40px rgba(15, 23, 42, 0.18);
    color: #222;
    background: #fff;
  }
  dialog.shortcuts::backdrop {
    background: rgba(15, 23, 42, 0.35);
  }
  .panel {
    display: flex;
    flex-direction: column;
    max-height: calc(100vh - 2rem);
  }
  header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.85rem 1.1rem;
    border-bottom: 1px solid #e8ecf0;
  }
  header h2 {
    margin: 0;
    font-size: 1.05rem;
    display: flex;
    align-items: center;
    gap: 0.45rem;
  }
  .page {
    font-size: 0.82rem;
    color: #666;
    padding: 0.1rem 0.5rem;
    border: 1px solid #e2e2e2;
    border-radius: 1rem;
    background: #f6f8fa;
  }
  .close {
    margin-left: auto;
    background: none;
    border: none;
    cursor: pointer;
    color: #666;
    font-size: 1rem;
    padding: 0.2rem;
    line-height: 1;
  }
  .close:hover {
    color: #222;
  }
  .groups {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 1.1rem 2rem;
    padding: 1rem 1.1rem;
    overflow-y: auto;
  }
  section h3 {
    margin: 0 0 0.5rem;
    font-size: 0.74rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #666;
  }
  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.4rem 0.9rem;
    align-items: baseline;
    margin: 0;
    font-size: 0.85rem;
  }
  dt {
    display: flex;
    align-items: baseline;
    gap: 0.25rem;
    white-space: nowrap;
  }
  dd {
    margin: 0;
    color: #333;
  }
  kbd {
    display: inline-block;
    min-width: 1.7em;
    padding: 0.08rem 0.4rem;
    border: 1px solid #cbd2d9;
    border-bottom-width: 2px;
    border-radius: 4px;
    background: #f6f8fa;
    color: #222;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.78rem;
    line-height: 1.4;
    text-align: center;
  }
  .then {
    font-size: 0.72rem;
    color: #888;
    padding: 0 0.15rem;
  }
  footer {
    padding: 0.7rem 1.1rem;
    border-top: 1px solid #e8ecf0;
    font-size: 0.78rem;
    color: #666;
    line-height: 1.7;
  }

  .hint {
    position: fixed;
    right: 1rem;
    bottom: 1rem;
    z-index: 20;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.6rem;
    border: 1px solid #d7dee6;
    border-radius: 2rem;
    background: #fff;
    color: #555;
    font: inherit;
    font-size: 0.8rem;
    cursor: pointer;
    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.12);
  }
  .hint:hover {
    color: #222;
    border-color: #b8c2cc;
  }
  @media (hover: none) and (pointer: coarse) {
    .hint {
      display: none;
    }
  }
</style>
