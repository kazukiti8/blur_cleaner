import numpy as np, cv2

def blur_score(path: str) -> float | None:
    # np.fromfileで日本語パス対応
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return float(cv2.Laplacian(img, cv2.CV_64F).var())
