<script lang="ts">
  import { RawProfileCard, storeSessionToken } from "gauntlet_fullstack/seed_frontend";

  export let summaryHtml: string;
  export let token: string;

  function remember() {
    storeSessionToken(token);
  }

  function bridge() {
    return RawProfileCard({ html: summaryHtml });
  }
</script>

<button on:click={remember}>remember</button>
<img src="/summary.png">
{@html summaryHtml}
