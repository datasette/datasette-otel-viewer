<script lang="ts">
  import { ICONS, type IconName } from "../lib/icons.ts";

  /**
   * One bootstrap icon from `lib/icons.ts`, sized in em so it follows the
   * surrounding text, coloured by `currentColor`. Decorative by default:
   * every use sits next to, or inside a control that already has, a text
   * label or an aria-label, so the SVG itself is hidden from readers.
   */
  const { name, size = "1em" }: { name: IconName; size?: string } = $props();
</script>

<svg
  xmlns="http://www.w3.org/2000/svg"
  width={size}
  height={size}
  viewBox="0 0 16 16"
  fill="currentColor"
  aria-hidden="true"
>
  <!-- Static path data from lib/icons.ts, never user input. -->
  {@html ICONS[name]}
</svg>

<style>
  svg {
    display: inline-block;
    vertical-align: -0.125em;
    flex-shrink: 0;
  }
</style>
