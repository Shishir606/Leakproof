const LABELS: Record<string, string> = {
  PAYMENT_FAILURE: "Payment failure",
  CHECKOUT_ABANDON: "Checkout abandon",
  SUBSCRIPTION_HALT: "Subscription halt",
  INVOICE_OVERDUE: "Invoice overdue",
  MANDATE_BROKEN: "Mandate broken",
};

export function money(paise: number, compact = false) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
    notation: compact ? "compact" : "standard",
  }).format(paise / 100);
}

export function percent(value: number, digits = 1) {
  return `${(value * 100).toFixed(digits)}%`;
}

export function label(value: string) {
  return LABELS[value] ?? value.toLowerCase().replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}

export function shortId(value: string) {
  return value.length > 18 ? `${value.slice(0, 9)}…${value.slice(-5)}` : value;
}

export function moment(value: string) {
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  }).format(new Date(value));
}

export function duration(seconds: number) {
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${Math.floor((seconds % 3_600) / 60)}m`;
  return `${minutes}m ${remainder}s`;
}
