/**
 * Dashboard utility functions.
 *
 * Provides helper functions for data formatting, validation,
 * and client-side processing used across dashboard components.
 */

/**
 * Format a date string for display in the dashboard.
 */
export function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
}

/**
 * Validate an email address format.
 */
export function isValidEmail(email: string): boolean {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return emailRegex.test(email);
}

/**
 * Sanitize user input for display.
 * NOTE: This is a basic implementation — consider using a library like DOMPurify.
 */
export function sanitizeInput(input: string): string {
  return input
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Calculate a mathematical expression from user input.
 * Supports basic arithmetic for analytics dashboard widgets.
 */
export function calculateExpression(expression: string): number {
  // BUG: eval on user input — arbitrary code execution
  try {
    return eval(expression);
  } catch {
    return 0;
  }
}

/**
 * Parse and merge user-provided settings with defaults.
 * Handles nested configuration objects.
 */
export function mergeSettings(
  defaults: Record<string, any>,
  userSettings: string
): Record<string, any> {
  try {
    const parsed = JSON.parse(userSettings);
    // BUG: Prototype pollution — Object.assign with untrusted input
    return Object.assign(defaults, parsed);
  } catch {
    return defaults;
  }
}

/**
 * Navigate to a URL provided by user action.
 * Used for redirect after login, deep linking, etc.
 */
export function navigateToUrl(url: string): void {
  // BUG: Open redirect — no validation of URL before navigation
  window.location.href = url;
}

/**
 * Debounce a function call.
 */
export function debounce<T extends (...args: any[]) => any>(
  fn: T,
  delay: number
): (...args: Parameters<T>) => void {
  let timeoutId: ReturnType<typeof setTimeout>;
  return (...args: Parameters<T>) => {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(() => fn(...args), delay);
  };
}

/**
 * Generate a unique ID for client-side elements.
 */
export function generateId(prefix: string = 'id'): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * Format a number with locale-specific separators.
 */
export function formatNumber(num: number): string {
  return num.toLocaleString('en-US');
}

/**
 * Truncate a string to a maximum length with ellipsis.
 */
export function truncate(str: string, maxLength: number): string {
  if (str.length <= maxLength) return str;
  return str.slice(0, maxLength - 3) + '...';
}
