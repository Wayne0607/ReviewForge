export function normalize(value: string): string {
  return value.trim().toLowerCase();
}

export function run(value: string): string {
  return normalize(value);
}
