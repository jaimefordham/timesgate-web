import io
import unittest

from PIL import Image, ImageChops

from timesgate.media import MAX_UPLOAD, encode_full_frames, normalize


class MediaTests(unittest.TestCase):
    def test_partial_transparent_animation_becomes_opaque_full_frames_without_trails(self):
        first = Image.new("RGBA", (128, 128))
        second = Image.new("RGBA", (128, 128))
        first.paste((255, 0, 0, 255), (4, 20, 20, 40))
        second.paste((0, 255, 0, 255), (50, 20, 66, 40))
        original = io.BytesIO()
        first.save(original, "GIF", save_all=True, append_images=[second],
                   duration=[90, 130], disposal=2, transparency=0)
        blob, count, duration = normalize(original.getvalue())
        self.assertEqual((count, duration), (2, 220))
        with Image.open(io.BytesIO(blob)) as output:
            for index in range(2):
                output.seek(index)
                self.assertEqual(output.tile[0][1], (0, 0, 128, 128))
                self.assertNotIn("transparency", output.info)
                self.assertEqual(output.disposal_method, 1)
            self.assertEqual(output.convert("RGB").getpixel((8, 24)), (0, 0, 0))
            self.assertEqual(output.convert("RGB").getpixel((54, 24)), (0, 255, 0))

    def test_independent_frame_palettes_preserve_colors_and_timing(self):
        frames = [Image.new("RGB", (128, 128), color) for color in ("red", "blue", "lime")]
        result = encode_full_frames(frames, [90, 100, 130])
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.n_frames, 3)
            for index, expected in enumerate(frames):
                image.seek(index)
                self.assertEqual(image.info["duration"], [90, 100, 130][index])
                self.assertIsNone(ImageChops.difference(image.convert("RGB"), expected).getbbox())

    def test_still_is_contained_without_stretching(self):
        source = io.BytesIO()
        Image.new("RGB", (128, 64), "red").save(source, "PNG")
        blob, count, _ = normalize(source.getvalue())
        with Image.open(io.BytesIO(blob)) as image:
            self.assertEqual(image.size, (128, 128))
            self.assertEqual(count, 1)
            rgb = image.convert("RGB")
            self.assertEqual(rgb.getpixel((64, 0)), (0, 0, 0))
            self.assertEqual(rgb.getpixel((64, 64)), (255, 0, 0))

    def test_invalid_or_excessive_upload_is_rejected(self):
        for data in (b"", b"not an image", b"x" * (MAX_UPLOAD + 1)):
            with self.assertRaises(ValueError):
                normalize(data)

    def test_frame_and_canvas_limits_are_enforced(self):
        frame = Image.new("RGB", (1, 1), "red")
        allowed = encode_full_frames([frame] * 300, [100] * 300)
        self.assertEqual(normalize(allowed)[1], 300)
        excessive = encode_full_frames([frame] * 301, [100] * 301)
        with self.assertRaisesRegex(ValueError, "300 frames"):
            normalize(excessive)
        canvas = io.BytesIO()
        Image.new("RGB", (2001, 2000)).save(canvas, "PNG")
        with self.assertRaisesRegex(ValueError, "4 megapixels"):
            normalize(canvas.getvalue())
