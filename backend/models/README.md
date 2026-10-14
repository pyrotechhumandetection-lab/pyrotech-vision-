# Model files (all required runtime files are in this folder)

| File | Used for |
|---|---|
| `yolov8n.pt` | Default detection model (COCO), same file as the original `human_detect_yolo-2.py` |
| `yolov8s.pt` | Optional larger model (slower, more accurate) — set `AIV_MODEL=models/yolov8s.pt` in `.env` |
| `bytetrack_stable.yaml` | ByteTrack tracker settings from the original project (tuned for fast/blurred CCTV objects) |
| `yolov8n_openvino_model/` | The same `yolov8n.pt` weights exported to OpenVINO (FP32, dynamic input shape) with Ultralytics 8.4.153. Used automatically when the `openvino` package is installed (`AIV_ENGINE=auto`); identical detections on CAM-02 test frames (29/29 boxes), about 3–4× faster on CPU. Without `openvino` the backend uses `yolov8n.pt` with PyTorch. |
| `head_yolov8m/` | **Human Head Count** head detector (single class `head`, YOLOv8m, trained at 1280 px on a crowd head dataset - heads seen from the front, side and back). Used only by Capture & Count (`backend/detection/head_count.py`). `head_yolov8m.pt.part0/1` = the original PyTorch weights; `head_yolov8m_openvino_model/` = the same weights exported to OpenVINO FP16 (dynamic shape, Ultralytics 8.4.153), identical detections on the test images (90/90 boxes) and about 6x faster on CPU. Large files are stored as `.part*` files (< 30 MB each) and joined automatically on first use (SHA-256 checked). |
| `fire_smoke/` | **Fire & Smoke Detection** (`backend/detection/fire_smoke.py`). `firedetect-11s.pt` = YOLO11s fire/smoke detector (classes `fire`, `smoke`), the *small* model the original fire & smoke project ran with (imgsz 480). `smoke_verifier.npz` = the original project's colour/texture smoke verifier (rejects grey walls, fog and lamp glare). Both are loaded only from this folder. |
| `fire_smoke/firedetect-11s_openvino_model/` | the same `firedetect-11s.pt` weights exported to OpenVINO (FP16 weights, dynamic input size, Ultralytics 8.4.155). Used automatically when the `openvino` package is installed (`AIV_FIRE_ENGINE=auto`). Measured on the fire test video: 19 ms instead of 95 ms per frame; per-frame fire/smoke decisions identical in 272 of 275 frames (the 3 others are fire boxes at confidence 0.300-0.305 vs 0.299 at the 0.30 threshold); real-time replay: identical alerts. The smoke verifier still uses the PyTorch weights. |
| `object_monitor/` | **Object Monitoring** (`backend/detection/object_monitor/`). `yoloe-26n-seg.pt` = Ultralytics YOLOE-26n open-vocabulary model used to name marked objects (Layer 2). `yoloe-26n-seg_vocab_87c9a6699f.npz` = the cached text embeddings of the default 23-name vocabulary, so no text encoder (MobileCLIP) or internet is needed. The vocabulary is therefore fixed to the defaults. Optional `hf/` = Grounding DINO for Layer 3 (not shipped, see below). |

`bytetrack_stable.yaml` also contains `vehicle_iou_buffer` (PYROTECH VISION addition, see `backend/detection/tracker.py`):
buffered IoU for vehicle-to-vehicle association so fast vehicles keep one track. `0` restores the original association.

## Head detector source and licence

`head_yolov8m` is the `best_re_final.pt` model published in the public GitHub repository
[AbelKidaneHaile/Reports](https://github.com/AbelKidaneHaile/Reports) ("Head Detector using YoloV8", trained on the
Kaggle *human-crowd-dataset*, reported mAP@0.5 about 0.78). SHA-256 of the joined files:

* `head_yolov8m.pt`  `ddcbf60f7639a6d1b8bba19ab5c4e31d0196a1fc195ee29a5e2a184453197d50`
* `head_yolov8m_openvino_model/head_yolov8m.bin`  `7c2af28b068d83640d243c13fb0511cb864cca938d5c49a0a7e89e1c9b3d5b3e`
* `fire_smoke/firedetect-11s_openvino_model/firedetect-11s.bin`  `16901c7e0aaf05d3e7baa41b5890208cb1e714424be4c13c5ef8a1c5d4422e13`

The repository does not state a licence; the model is built with Ultralytics YOLOv8 (AGPL-3.0). Check both before
shipping it in a commercial product, or replace it with a head model trained on your own camera images
(same file names, single class `head`).

## Fire & Smoke and Object Monitoring model sources

* `fire_smoke/firedetect-11s.pt` - Hugging Face `leeyunjai/yolo11-firedetect` (copied from the original fire & smoke project), Ultralytics YOLO11 (AGPL-3.0).
  SHA-256 `3009bc8cdc81d879ca3e48ca8c37f6cb9bffa505a3ef474247a73a38fa81d0bf`.
* `fire_smoke/smoke_verifier.npz` - built by the original fire & smoke project; SHA-256 checked on load.
* `object_monitor/yoloe-26n-seg.pt` - Ultralytics YOLOE-26 (AGPL-3.0), copied from the original object detection project. SHA-256 `1741c1f8da3cea47e2c01829c334a50dc0b9bbd05e685b90a3ce84fae32c8c1b`.
* Grounding DINO (Layer 3 name check, Apache-2.0) is **not shipped** (about 690 MB). Object Monitoring works without it.
  To add it, copy the original project's Grounding DINO model folder with `backend\tools\install-grounding-dino.cmd <folder>`
  (copies it into `backend/models/object_monitor/hf/` and installs `transformers`).
