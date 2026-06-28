# Segment Anything 故障排查指南

## 安装问题

### CUDA 不可用

**错误**：`RuntimeError: CUDA not available`

**解决方案**：
```python
# 检查 CUDA 可用性
import torch
print(torch.cuda.is_available())
print(torch.version.cuda)

# 安装带 CUDA 的 PyTorch
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# 如果 CUDA 可用但 SAM 未使用它
sam = sam_model_registry["vit_h"](checkpoint="sam_vit_h_4b8939.pth")
sam.to("cuda")  # 显式地移到 GPU
```

### 导入错误

**错误**：`ModuleNotFoundError: No module named 'segment_anything'`

**解决方案**：
```bash
# 从 GitHub 安装
pip install git+https://github.com/facebookresearch/segment-anything.git

# 或克隆后安装
git clone https://github.com/facebookresearch/segment-anything.git
cd segment-anything
pip install -e .

# 验证安装
python -c "from segment_anything import sam_model_registry; print('OK')"
```

### 缺少依赖

**错误**：`ModuleNotFoundError: No module named 'cv2'` 或类似

**解决方案**：
```bash
# 安装所有可选依赖
pip install opencv-python pycocotools matplotlib onnxruntime onnx

# Windows 上的 pycocotools
pip install pycocotools-windows
```

## 模型加载问题

### 找不到检查点

**错误**：`FileNotFoundError: checkpoint file not found`

**解决方案**：
```bash
# 下载正确的检查点
wget https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth

# 校验文件完整性
md5sum sam_vit_h_4b8939.pth
# 期望值：a7bf3b02f3ebf1267aba913ff637d9a2

# 使用绝对路径
sam = sam_model_registry["vit_h"](checkpoint="/full/path/to/sam_vit_h_4b8939.pth")
```

### 模型类型不匹配

**错误**：`KeyError: 'unexpected key in state_dict'`

**解决方案**：
```python
# 确保模型类型与检查点匹配
# vit_h 检查点 → vit_h 模型
sam = sam_model_registry["vit_h"](checkpoint="sam_vit_h_4b8939.pth")

# vit_l 检查点 → vit_l 模型
sam = sam_model_registry["vit_l"](checkpoint="sam_vit_l_0b3195.pth")

# vit_b 检查点 → vit_b 模型
sam = sam_model_registry["vit_b"](checkpoint="sam_vit_b_01ec64.pth")
```

### 加载时显存不足

**错误**：模型加载期间出现 `CUDA out of memory`

**解决方案**：
```python
# 使用更小的模型
sam = sam_model_registry["vit_b"](checkpoint="sam_vit_b_01ec64.pth")

# 先加载到 CPU，再迁移
sam = sam_model_registry["vit_h"](checkpoint="sam_vit_h_4b8939.pth")
sam.to("cpu")
torch.cuda.empty_cache()
sam.to("cuda")

# 使用半精度
sam = sam_model_registry["vit_h"](checkpoint="sam_vit_h_4b8939.pth")
sam = sam.half()
sam.to("cuda")
```

## 推理问题

### 图像格式错误

**错误**：`ValueError: expected input to have 3 channels`

**解决方案**：
```python
import cv2

# 确保 RGB 格式
image = cv2.imread("image.jpg")
image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)  # BGR 转 RGB

# 灰度图转 RGB
if len(image.shape) == 2:
    image = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)

# 处理 RGBA
if image.shape[2] == 4:
    image = image[:, :, :3]  # 丢弃 alpha 通道
```

### 坐标错误

**错误**：`IndexError: index out of bounds` 或掩码位置不对

**解决方案**：
```python
# 确保点是 (x, y) 而非 (row, col)
# x = 列索引，y = 行索引
point = np.array([[x, y]])  # 正确

# 校验坐标在图像范围内
h, w = image.shape[:2]
assert 0 <= x < w and 0 <= y < h, "Point outside image"

# 对于边界框：[x1, y1, x2, y2]
box = np.array([x1, y1, x2, y2])
assert x1 < x2 and y1 < y2, "Invalid box coordinates"
```

### 掩码为空或不正确

**问题**：掩码与预期对象不匹配

**解决方案**：
```python
# 尝试多个提示
input_points = np.array([[x1, y1], [x2, y2]])
input_labels = np.array([1, 1])  # 多个前景点

# 添加背景点
input_points = np.array([[obj_x, obj_y], [bg_x, bg_y]])
input_labels = np.array([1, 0])  # 1=前景，0=背景

# 对大对象使用框提示
box = np.array([x1, y1, x2, y2])
masks, scores, _ = predictor.predict(box=box, multimask_output=False)

# 组合框和点
masks, scores, _ = predictor.predict(
    point_coords=np.array([[center_x, center_y]]),
    point_labels=np.array([1]),
    box=np.array([x1, y1, x2, y2]),
    multimask_output=True
)

# 检查分数并选择最佳
print(f"Scores: {scores}")
best_mask = masks[np.argmax(scores)]
```

### 推理慢

**问题**：预测耗时过长

**解决方案**：
```python
# 使用更小的模型
sam = sam_model_registry["vit_b"](checkpoint="sam_vit_b_01ec64.pth")

# 复用图像嵌入
predictor.set_image(image)  # 只计算一次
for point in points:
    masks, _, _ = predictor.predict(...)  # 很快，复用嵌入

# 减少自动生成的点数
mask_generator = SamAutomaticMaskGenerator(
    model=sam,
    points_per_side=16,  # 默认是 32
)

# 部署时使用 ONNX
# 导出：python scripts/export_onnx_model.py --return-single-mask
```

## 自动掩码生成问题

### 掩码过多

**问题**：生成了数千个重叠掩码

**解决方案**：
```python
mask_generator = SamAutomaticMaskGenerator(
    model=sam,
    points_per_side=16,          # 从 32 减小
    pred_iou_thresh=0.92,        # 从 0.88 提高
    stability_score_thresh=0.98,  # 从 0.95 提高
    box_nms_thresh=0.5,          # 更激进的 NMS
    min_mask_region_area=500,    # 移除小掩码
)
```

### 掩码过少

**问题**：自动生成中遗漏了对象

**解决方案**：
```python
mask_generator = SamAutomaticMaskGenerator(
    model=sam,
    points_per_side=64,          # 增大密度
    pred_iou_thresh=0.80,        # 降低阈值
    stability_score_thresh=0.85,  # 降低阈值
    crop_n_layers=2,             # 添加多尺度
    min_mask_region_area=0,      # 保留所有掩码
)
```

### 漏掉小对象

**问题**：自动生成遗漏了小对象

**解决方案**：
```python
# 用裁剪层做多尺度检测
mask_generator = SamAutomaticMaskGenerator(
    model=sam,
    crop_n_layers=2,
    crop_n_points_downscale_factor=1,  # 裁剪中不减点
    min_mask_region_area=10,  # 非常小的最小值
)

# 或处理图像分块
def segment_with_patches(image, patch_size=512, overlap=64):
    h, w = image.shape[:2]
    all_masks = []

    for y in range(0, h, patch_size - overlap):
        for x in range(0, w, patch_size - overlap):
            patch = image[y:y+patch_size, x:x+patch_size]
            masks = mask_generator.generate(patch)

            # 把掩码偏移回原坐标
            for m in masks:
                m['bbox'][0] += x
                m['bbox'][1] += y
                # 也偏移分割掩码

            all_masks.extend(masks)

    return all_masks
```

## 显存问题

### CUDA 显存不足

**错误**：`torch.cuda.OutOfMemoryError: CUDA out of memory`

**解决方案**：
```python
# 使用更小的模型
sam = sam_model_registry["vit_b"](checkpoint="sam_vit_b_01ec64.pth")

# 在图像之间清空缓存
torch.cuda.empty_cache()

# 串行而非批量处理图像
for image in images:
    predictor.set_image(image)
    masks, _, _ = predictor.predict(...)
    torch.cuda.empty_cache()

# 缩小图像尺寸
max_size = 1024
h, w = image.shape[:2]
if max(h, w) > max_size:
    scale = max_size / max(h, w)
    image = cv2.resize(image, (int(w*scale), int(h*scale)))

# 大批量处理时使用 CPU
sam.to("cpu")
```

### RAM 不足

**问题**：系统内存耗尽

**解决方案**：
```python
# 一次处理一张图像
for img_path in image_paths:
    image = cv2.imread(img_path)
    masks = process_image(image)
    save_results(masks)
    del image, masks
    gc.collect()

# 使用生成器而非列表
def generate_masks_lazy(image_paths):
    for path in image_paths:
        image = cv2.imread(path)
        masks = mask_generator.generate(image)
        yield path, masks
```

## ONNX 导出问题

### 导出失败

**错误**：各种导出错误

**解决方案**：
```bash
# 安装正确的 ONNX 版本
pip install onnx==1.14.0 onnxruntime==1.15.0

# 使用正确的 opset 版本
python scripts/export_onnx_model.py \
    --checkpoint sam_vit_h_4b8939.pth \
    --model-type vit_h \
    --output sam.onnx \
    --opset 17
```

### ONNX 运行时错误

**错误**：推理期间出现 `ONNXRuntimeError`

**解决方案**：
```python
import onnxruntime

# 检查可用的 provider
print(onnxruntime.get_available_providers())

# 如果 GPU 失败，使用 CPU provider
session = onnxruntime.InferenceSession(
    "sam.onnx",
    providers=['CPUExecutionProvider']
)

# 校验输入形状
for input in session.get_inputs():
    print(f"{input.name}: {input.shape}")
```

## HuggingFace 集成问题

### Processor 错误

**错误**：SamProcessor 相关问题

**解决方案**：
```python
from transformers import SamModel, SamProcessor

# 使用匹配的 processor 和模型
model = SamModel.from_pretrained("facebook/sam-vit-huge")
processor = SamProcessor.from_pretrained("facebook/sam-vit-huge")

# 确保输入格式正确
input_points = [[[x, y]]]  # 为批次维度嵌套的列表
inputs = processor(image, input_points=input_points, return_tensors="pt")

# 正确地后处理
masks = processor.image_processor.post_process_masks(
    outputs.pred_masks.cpu(),
    inputs["original_sizes"].cpu(),
    inputs["reshaped_input_sizes"].cpu()
)
```

## 质量问题

### 掩码边缘锯齿

**问题**：掩码边缘粗糙、呈像素化

**解决方案**：
```python
import cv2
from scipy import ndimage

def smooth_mask(mask, sigma=2):
    """平滑掩码边缘。"""
    # 高斯模糊
    smooth = ndimage.gaussian_filter(mask.astype(float), sigma=sigma)
    return smooth > 0.5

def refine_edges(mask, kernel_size=5):
    """用形态学运算精修掩码边缘。"""
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    # 闭合小间隙
    closed = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    # 开运算去除噪声
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, kernel)
    return opened.astype(bool)
```

### 分割不完整

**问题**：掩码未覆盖整个对象

**解决方案**：
```python
# 添加多个点
input_points = np.array([
    [obj_center_x, obj_center_y],
    [obj_left_x, obj_center_y],
    [obj_right_x, obj_center_y],
    [obj_center_x, obj_top_y],
    [obj_center_x, obj_bottom_y]
])
input_labels = np.array([1, 1, 1, 1, 1])

# 使用边界框
masks, _, _ = predictor.predict(
    box=np.array([x1, y1, x2, y2]),
    multimask_output=False
)

# 迭代精修
mask_input = None
for point in points:
    masks, scores, logits = predictor.predict(
        point_coords=point.reshape(1, 2),
        point_labels=np.array([1]),
        mask_input=mask_input,
        multimask_output=False
    )
    mask_input = logits
```

## 常见错误信息

| 错误 | 原因 | 解决方案 |
|-------|-------|----------|
| `CUDA out of memory` | GPU 显存已满 | 使用更小的模型，清空缓存 |
| `expected 3 channels` | 图像格式错误 | 转换为 RGB |
| `index out of bounds` | 坐标无效 | 检查点/框的范围 |
| `checkpoint not found` | 路径错误 | 使用绝对路径 |
| `unexpected key` | 模型/检查点不匹配 | 匹配模型类型 |
| `invalid box coordinates` | x1 > x2 或 y1 > y2 | 修正框格式 |

## 获取帮助

1. **GitHub Issues**：https://github.com/facebookresearch/segment-anything/issues
2. **HuggingFace 论坛**：https://discuss.huggingface.co
3. **论文**：https://arxiv.org/abs/2304.02643

### 报告问题

请包含：
- Python 版本
- PyTorch 版本：`python -c "import torch; print(torch.__version__)"`
- CUDA 版本：`python -c "import torch; print(torch.version.cuda)"`
- SAM 模型类型（vit_b/l/h）
- 完整的错误回溯
- 最小可复现代码
