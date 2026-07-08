<script lang="ts">
  let userHtml = '<span>User input</span>';
  let apiKey = 'sk-svelte-secret-789'; // BUG: hardcoded secret

  function processTemplate(input: string) {
    // BUG: eval in frontend code
    const result = eval(`(${input})`);
    return result;
  }

  function updateContent(html: string) {
    userHtml = html;
  }
</script>

<div>
  <!-- BUG: XSS via {@html} -->
  {@html userHtml}

  <button on:click={() => updateContent(userHtml)}>
    Refresh
  </button>
</div>
