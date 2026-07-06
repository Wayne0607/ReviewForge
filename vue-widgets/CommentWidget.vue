<template>
  <div class="comment-widget">
    <h3>Comments ({{ comments.length }})</h3>

    <div class="comment-list">
      <div
        v-for="comment in comments"
        :key="comment.id"
        class="comment-item"
      >
        <div class="comment-header">
          <strong>{{ comment.author }}</strong>
          <span class="comment-date">{{ formatDate(comment.created_at) }}</span>
        </div>
        <!-- BUG: XSS via v-html — renders raw HTML from user comments -->
        <div class="comment-body" v-html="comment.content"></div>
        <div class="comment-actions">
          <!-- BUG: Clickable div without keyboard accessibility -->
          <div
            class="action-link"
            @click="replyTo(comment)"
          >
            Reply
          </div>
          <div
            class="action-link"
            @click="likeComment(comment)"
          >
            Like ({{ comment.likes }})
          </div>
        </div>
      </div>
    </div>

    <form @submit.prevent="submitComment" class="comment-form">
      <textarea
        v-model="newComment"
        placeholder="Write a comment..."
        rows="3"
      ></textarea>
      <button type="submit" :disabled="!newComment.trim()">
        Post Comment
      </button>
    </form>
  </div>
</template>

<script>
export default {
  name: 'CommentWidget',

  props: {
    postId: {
      type: [Number, String],
      required: true,
    },
  },

  data() {
    return {
      comments: [],
      newComment: '',
    };
  },

  mounted() {
    this.fetchComments();
  },

  methods: {
    async fetchComments() {
      try {
        const response = await fetch(`/api/posts/${this.postId}/comments`);
        this.comments = await response.json();
      } catch (error) {
        console.error('Failed to load comments:', error);
      }
    },

    async submitComment() {
      if (!this.newComment.trim()) return;

      try {
        const response = await fetch(`/api/posts/${this.postId}/comments`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // BUG: No CSRF token in POST request
          body: JSON.stringify({ content: this.newComment }),
        });

        if (response.ok) {
          const comment = await response.json();
          this.comments.push(comment);
          this.newComment = '';
        }
      } catch (error) {
        console.error('Failed to post comment:', error);
      }
    },

    replyTo(comment) {
      this.newComment = `@${comment.author} `;
    },

    likeComment(comment) {
      comment.likes++;
      // Fire and forget — no error handling
      fetch(`/api/comments/${comment.id}/like`, { method: 'POST' });
    },

    formatDate(dateStr) {
      return new Date(dateStr).toLocaleDateString();
    },
  },
};
</script>

<style scoped>
.comment-widget {
  max-width: 600px;
  margin: 0 auto;
}
.comment-item {
  border-bottom: 1px solid #eee;
  padding: 12px 0;
}
.comment-header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
}
.comment-body {
  line-height: 1.5;
}
.comment-actions {
  display: flex;
  gap: 16px;
  margin-top: 8px;
}
.action-link {
  color: #4a90d9;
  cursor: pointer;
  font-size: 14px;
}
.action-link:hover {
  text-decoration: underline;
}
.comment-form {
  margin-top: 16px;
}
.comment-form textarea {
  width: 100%;
  resize: vertical;
}
.comment-form button {
  margin-top: 8px;
}
</style>
