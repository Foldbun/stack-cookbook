// Copied from clients/go/ in github.com/Foldbun/stack-cookbook. Edit the original there.

package stackclient

import (
	"errors"
	"fmt"
)

// Bitmap is a binary mask on its own grid, row-major: Pix[row*Width+col] is 1 for an object pixel.
type Bitmap struct {
	Height, Width int
	Pix           []uint8
}

// NewBitmap returns an all-background bitmap.
func NewBitmap(height, width int) *Bitmap {
	return &Bitmap{Height: height, Width: width, Pix: make([]uint8, height*width)}
}

// At reports whether the pixel in the given row and column is an object pixel.
func (b *Bitmap) At(row, col int) bool { return b.Pix[row*b.Width+col] != 0 }

// DecodeCOCORLE turns a COCO compressed RLE string (the `counts` of a `coco` mask) into a bitmap. height and
// width are the two numbers of `size`, in that order. The runs are column-major and start with background.
func DecodeCOCORLE(height, width int, counts string) (*Bitmap, error) {
	runs, err := RunsFromString(counts)
	if err != nil {
		return nil, err
	}
	b := NewBitmap(height, width)
	pos, value := 0, uint8(0)
	for _, r := range runs {
		if r < 0 || pos+r > len(b.Pix) {
			return nil, fmt.Errorf("rle: runs cover more than the %dx%d grid", height, width)
		}
		if value == 1 {
			for i := pos; i < pos+r; i++ {
				b.Pix[(i%height)*width+i/height] = 1 // index i runs down the columns
			}
		}
		pos, value = pos+r, 1-value
	}
	if pos != len(b.Pix) {
		return nil, fmt.Errorf("rle: runs cover %d of the %d pixels of a %dx%d grid", pos, len(b.Pix), height, width)
	}
	return b, nil
}

// EncodeCOCORLE is the inverse of DecodeCOCORLE: the COCO compressed RLE string of the bitmap.
func EncodeCOCORLE(b *Bitmap) string {
	runs, value, run := []int{}, uint8(0), 0
	for col := 0; col < b.Width; col++ {
		for row := 0; row < b.Height; row++ {
			v := b.Pix[row*b.Width+col]
			if v != 0 {
				v = 1
			}
			if v != value {
				runs, value, run = append(runs, run), v, 0
			}
			run++
		}
	}
	return RunsToString(append(runs, run))
}

// RunsFromString decodes the counts string into alternating run lengths (background first).
func RunsFromString(s string) ([]int, error) {
	runs := []int{}
	for p := 0; p < len(s); {
		x, k := 0, 0
		for more := true; more; k++ {
			if p >= len(s) {
				return nil, errors.New("rle: counts string ends inside a number")
			}
			c := int(s[p]) - 48
			if c < 0 || c >= 64 || k > 11 {
				return nil, fmt.Errorf("rle: invalid counts string at byte %d", p)
			}
			x |= (c & 0x1f) << (5 * k)
			more = c&0x20 != 0
			p++
			if !more && c&0x10 != 0 {
				x |= -1 << (5 * (k + 1)) // sign-extend the last group
			}
		}
		if len(runs) > 2 {
			x += runs[len(runs)-2] // runs from the third on are stored as the difference to the one before the last
		}
		runs = append(runs, x)
	}
	return runs, nil
}

// RunsToString encodes alternating run lengths (background first) as a COCO counts string.
func RunsToString(runs []int) string {
	out := make([]byte, 0, len(runs)*2)
	for i, x := range runs {
		if i > 2 {
			x -= runs[i-2]
		}
		for more := true; more; {
			c := x & 0x1f
			x >>= 5
			if c&0x10 != 0 {
				more = x != -1
			} else {
				more = x != 0
			}
			if more {
				c |= 0x20
			}
			out = append(out, byte(c+48))
		}
	}
	return string(out)
}
