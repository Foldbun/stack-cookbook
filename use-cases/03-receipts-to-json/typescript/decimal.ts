// Exact decimal numbers for money. A JavaScript number cannot hold 0.1 + 0.2 exactly, so every amount is parsed and
// summed as a scaled integer (BigInt) and turned into a number only when it goes into the output.

const POW10: bigint[] = [1n];
function pow10(n: number): bigint {
  while (POW10.length <= n) POW10.push(POW10[POW10.length - 1] * 10n);
  return POW10[n];
}

/** units / 10^scale, with the scale the number was written with ("16.00" has scale 2). */
export class Dec {
  readonly units: bigint;
  readonly scale: number;

  constructor(units: bigint, scale = 0) {
    this.units = units;
    this.scale = scale;
  }

  /** "1234.50", "-7", "1e-7"; throws on anything else. */
  static parse(text: string): Dec {
    const m = /^([+-]?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(text.trim());
    if (!m) throw new SyntaxError(`not a decimal number: ${text}`);
    const fraction = m[3] ?? "";
    let units = BigInt(m[2] + fraction);
    let scale = fraction.length - Number(m[4] ?? 0);
    if (scale < 0) {
      units *= pow10(-scale);
      scale = 0;
    }
    return new Dec(m[1] === "-" ? -units : units, scale);
  }

  /** The decimal of a JSON number, through its shortest printed form (what Python's `Decimal(str(x))` does). */
  static of(x: number | bigint | string): Dec {
    return Dec.parse(String(x));
  }

  private aligned(other: Dec): [bigint, bigint, number] {
    const scale = Math.max(this.scale, other.scale);
    return [this.units * pow10(scale - this.scale), other.units * pow10(scale - other.scale), scale];
  }

  plus(other: Dec): Dec {
    const [a, b, scale] = this.aligned(other);
    return new Dec(a + b, scale);
  }

  minus(other: Dec): Dec {
    const [a, b, scale] = this.aligned(other);
    return new Dec(a - b, scale);
  }

  times(other: Dec): Dec {
    return new Dec(this.units * other.units, this.scale + other.scale);
  }

  neg(): Dec {
    return new Dec(-this.units, this.scale);
  }

  abs(): Dec {
    return this.units < 0n ? this.neg() : this;
  }

  /** -1, 0 or 1. */
  cmp(other: Dec): number {
    const [a, b] = this.aligned(other);
    return a < b ? -1 : a > b ? 1 : 0;
  }

  eq(other: Dec): boolean {
    return this.cmp(other) === 0;
  }

  isInteger(): boolean {
    return this.units % pow10(this.scale) === 0n;
  }

  toString(): string {
    const negative = this.units < 0n;
    const digits = (negative ? -this.units : this.units).toString().padStart(this.scale + 1, "0");
    const cut = digits.length - this.scale;
    return (negative ? "-" : "") + (this.scale ? `${digits.slice(0, cut)}.${digits.slice(cut)}` : digits);
  }

  /** The closest number; exact for every amount a receipt prints. */
  toNumber(): number {
    return Number(this.toString());
  }
}

export const ZERO = new Dec(0n);
