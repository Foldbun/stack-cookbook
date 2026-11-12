package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"
	_ "image/png"
	"os"

	"stack-cookbook/01-dataset-prelabelling/stackclient"
)

// Crops for the review queue, and the two mask helpers the COCO export needs. Standard library only: JPEG and PNG
// images can be cropped; other formats (WebP) are segmented as usual but their review rows get no crop.

// saveCrop writes a JPEG crop of `box` with some context (10% of its longer side); no box means the whole image. The
// longer side of the crop is at most 384 px. The image is turned upright by its EXIF orientation first, because the
// API's coordinates refer to the upright image.
func saveCrop(src string, box *[4]int, dest string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return err
	}
	img = upright(img, exifOrientation(data))
	b := img.Bounds()
	if box != nil {
		m := int(0.1 * float64(max(box[2]-box[0], box[3]-box[1])))
		r := image.Rect(max(0, box[0]-m), max(0, box[1]-m), min(b.Dx(), box[2]+m), min(b.Dy(), box[3]+m))
		img = img.(interface {
			SubImage(image.Rectangle) image.Image
		}).SubImage(r.Add(b.Min))
	}
	var out bytes.Buffer
	if err := jpeg.Encode(&out, shrink(img, 384), &jpeg.Options{Quality: 85}); err != nil {
		return err
	}
	return os.WriteFile(dest, out.Bytes(), 0o644)
}

// upright returns img rotated or flipped as the EXIF orientation (1 to 8) says; an upright image is returned as it is.
func upright(img image.Image, orientation int) image.Image {
	if orientation <= 1 {
		return img
	}
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	dw, dh := w, h
	if orientation >= 5 {
		dw, dh = h, w
	}
	out := image.NewNRGBA(image.Rect(0, 0, dw, dh))
	for y := 0; y < dh; y++ {
		for x := 0; x < dw; x++ {
			sx, sy := x, y // orientation 1: unchanged
			switch orientation {
			case 2:
				sx = w - 1 - x
			case 3:
				sx, sy = w-1-x, h-1-y
			case 4:
				sy = h - 1 - y
			case 5:
				sx, sy = y, x
			case 6:
				sx, sy = y, h-1-x
			case 7:
				sx, sy = w-1-y, h-1-x
			case 8:
				sx, sy = w-1-y, x
			}
			out.Set(x, y, img.At(b.Min.X+sx, b.Min.Y+sy))
		}
	}
	return out
}

// exifOrientation reads the orientation tag of a JPEG; 1 (upright) when there is none.
func exifOrientation(data []byte) int {
	if len(data) < 4 || data[0] != 0xFF || data[1] != 0xD8 {
		return 1
	}
	for pos := 2; pos+4 <= len(data) && data[pos] == 0xFF; {
		marker, size := data[pos+1], int(binary.BigEndian.Uint16(data[pos+2:]))
		if marker == 0xDA || size < 2 || pos+2+size > len(data) {
			break // start of the image data
		}
		seg := data[pos+4 : pos+2+size]
		if marker == 0xE1 && len(seg) > 14 && string(seg[:6]) == "Exif\x00\x00" {
			tiff := seg[6:]
			var order binary.ByteOrder = binary.BigEndian
			if tiff[0] == 'I' {
				order = binary.LittleEndian
			}
			ifd := int(order.Uint32(tiff[4:]))
			if ifd+2 > len(tiff) {
				break
			}
			for i, n := 0, int(order.Uint16(tiff[ifd:])); i < n && ifd+2+12*(i+1) <= len(tiff); i++ {
				entry := tiff[ifd+2+12*i:]
				if order.Uint16(entry) == 0x0112 {
					if v := int(order.Uint16(entry[8:])); v >= 1 && v <= 8 {
						return v
					}
				}
			}
			break
		}
		pos += 2 + size
	}
	return 1
}

// shrink scales img down (bilinear) so that its longer side is at most `longest`; smaller images are left alone.
func shrink(img image.Image, longest int) image.Image {
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	if max(w, h) <= longest {
		return img
	}
	scale := float64(longest) / float64(max(w, h))
	dw, dh := max(1, int(float64(w)*scale)), max(1, int(float64(h)*scale))
	src := image.NewNRGBA(image.Rect(0, 0, w, h))
	draw.Draw(src, src.Bounds(), img, b.Min, draw.Src)
	out := image.NewNRGBA(image.Rect(0, 0, dw, dh))
	for y := 0; y < dh; y++ {
		fy := min(float64(h-1), max(0, (float64(y)+0.5)/scale-0.5))
		y0 := int(fy)
		y1 := min(h-1, y0+1)
		ty := fy - float64(y0)
		for x := 0; x < dw; x++ {
			fx := min(float64(w-1), max(0, (float64(x)+0.5)/scale-0.5))
			x0 := int(fx)
			x1 := min(w-1, x0+1)
			tx := fx - float64(x0)
			var px [4]uint8
			for c := 0; c < 4; c++ {
				top := float64(src.Pix[src.PixOffset(x0, y0)+c])*(1-tx) + float64(src.Pix[src.PixOffset(x1, y0)+c])*tx
				bot := float64(src.Pix[src.PixOffset(x0, y1)+c])*(1-tx) + float64(src.Pix[src.PixOffset(x1, y1)+c])*tx
				px[c] = uint8(top*(1-ty) + bot*ty + 0.5)
			}
			out.SetNRGBA(x, y, color.NRGBA{px[0], px[1], px[2], px[3]})
		}
	}
	return out
}

// fitMask returns the mask on an image-sized grid: a mask on a smaller grid is resampled (nearest neighbour).
func fitMask(m rle, height, width int) (rle, error) {
	if m.Size == [2]int{height, width} {
		return m, nil
	}
	src, err := stackclient.DecodeCOCORLE(m.Size[0], m.Size[1], m.Counts)
	if err != nil {
		return m, fmt.Errorf("mask: %w", err)
	}
	h, w := m.Size[0], m.Size[1]
	out := stackclient.NewBitmap(height, width)
	for y := 0; y < height; y++ {
		sy := min(h-1, int((float64(y)+0.5)*float64(h)/float64(height)))
		for x := 0; x < width; x++ {
			sx := min(w-1, int((float64(x)+0.5)*float64(w)/float64(width)))
			out.Pix[y*width+x] = src.Pix[sy*w+sx]
		}
	}
	return rle{Encoding: m.Encoding, Size: [2]int{height, width}, Counts: stackclient.EncodeCOCORLE(out)}, nil
}

// rleArea is the number of mask pixels, from the runs alone (every second run counts object pixels).
func rleArea(m rle) (int, error) {
	runs, err := stackclient.RunsFromString(m.Counts)
	if err != nil {
		return 0, err
	}
	area := 0
	for i := 1; i < len(runs); i += 2 {
		area += runs[i]
	}
	return area, nil
}
