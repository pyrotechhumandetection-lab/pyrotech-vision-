"""Fine-tune the rider helmet model on the merged dataset (run on a GPU machine, e.g. a Colab T4; ~1-3 h).

    python3 train.py --data data/twhelmet_in/data.yaml --base ../../models/twhelmet/rider_helmet.pt --name run1

Starting from the shipped rider_helmet.pt keeps what it already knows and adapts it; the dataset decides what is new.
Settings are chosen for the failure modes seen on Indian roads (small / far riders, motion blur, partial riders at the frame edge,
sun and shadow, dust, many riders in one picture): large input (960), strong scale + translate augmentation, HSV (sun / shade),
mosaic (many small riders), close_mosaic for the last epochs. Nothing replaces the installed model: the result is
runs/<name>/weights/best.pt - evaluate it first (merge_datasets.py puts your own-camera photos into the validation set; then python3 ../eval_twhelmet.py), then copy it to backend/models/twhelmet/rider_helmet.pt yourself."""
import argparse

from ultralytics import YOLO


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--base", default="yolov8s.pt", help="start weights (rider_helmet.pt to fine-tune the shipped model; yolov8s.pt to start fresh)")
    ap.add_argument("--name", default="twhelmet")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default="0")
    a = ap.parse_args()
    m = YOLO(a.base)
    m.train(data=a.data, epochs=a.epochs, imgsz=a.imgsz, batch=a.batch, device=a.device, name=a.name, patience=20,
            scale=0.6, translate=0.15, degrees=4, shear=2, perspective=0.0005, fliplr=0.5, flipud=0.0,
            hsv_h=0.02, hsv_s=0.7, hsv_v=0.5, mosaic=1.0, mixup=0.1, close_mosaic=12, copy_paste=0.0,
            cos_lr=True, lr0=0.003, warmup_epochs=2, cache=True, workers=4, plots=True)
    r = m.val(data=a.data, imgsz=a.imgsz, device=a.device, conf=0.001, iou=0.6)
    names = r.names
    print("\nvalidation (the 'val' folder of the dataset):")
    for i, n in names.items():
        print(f"  {n:<14} mAP50 {r.box.ap50[i]:.3f}   mAP50-95 {r.box.ap[i]:.3f}")
    print("weights:", m.trainer.best)


if __name__ == "__main__":
    main()
