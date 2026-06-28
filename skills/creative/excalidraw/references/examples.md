# Excalidraw 图表示例

完整的、可直接复制粘贴的示例。保存前请将每个示例包裹在 `.excalidraw` 信封中：

```json
{
  "type": "excalidraw",
  "version": 2,
  "source": "hermes-agent",
  "elements": [ ...下面示例中的元素... ],
  "appState": { "viewBackgroundColor": "#ffffff" }
}
```

> **重要：** 形状和箭头上的所有文本标签都使用容器绑定（`containerId` + `boundElements`）。
> 不要使用不存在的 `"label"` 属性——它会被静默忽略，从而产生空白形状。

---

## 示例 1：两个相连的带标签方框

一个最小化的流程图，包含两个方框以及它们之间的一条箭头。

```json
[
  { "type": "text", "id": "title", "x": 280, "y": 30, "text": "Simple Flow", "fontSize": 28, "fontFamily": 1, "strokeColor": "#1e1e1e", "originalText": "Simple Flow", "autoResize": true },
  { "type": "rectangle", "id": "b1", "x": 100, "y": 100, "width": 200, "height": 100, "roundness": { "type": 3 }, "backgroundColor": "#a5d8ff", "fillStyle": "solid", "boundElements": [{ "id": "t_b1", "type": "text" }, { "id": "a1", "type": "arrow" }] },
  { "type": "text", "id": "t_b1", "x": 105, "y": 130, "width": 190, "height": 25, "text": "Start", "fontSize": 20, "fontFamily": 1, "strokeColor": "#1e1e1e", "textAlign": "center", "verticalAlign": "middle", "containerId": "b1", "originalText": "Start", "autoResize": true },
  { "type": "rectangle", "id": "b2", "x": 450, "y": 100, "width": 200, "height": 100, "roundness": { "type": 3 }, "backgroundColor": "#b2f2bb", "fillStyle": "solid", "boundElements": [{ "id": "t_b2", "type": "text" }, { "id": "a1", "type": "arrow" }] },
  { "type": "text", "id": "t_b2", "x": 455, "y": 130, "width": 190, "height": 25, "text": "End", "fontSize": 20, "fontFamily": 1, "strokeColor": "#1e1e1e", "textAlign": "center", "verticalAlign": "middle", "containerId": "b2", "originalText": "End", "autoResize": true },
  { "type": "arrow", "id": "a1", "x": 300, "y": 150, "width": 150, "height": 0, "points": [[0,0],[150,0]], "endArrowhead": "arrow", "startBinding": { "elementId": "b1", "fixedPoint": [1, 0.5] }, "endBinding": { "elementId": "b2", "fixedPoint": [0, 0.5] } }
]
```

---

## 示例 2：光合作用过程图

一张更大的图表，包含背景区域、多个节点以及显示输入/输出的方向箭头。

```json
[
  {"type":"text","id":"ti","x":280,"y":10,"text":"Photosynthesis","fontSize":28,"fontFamily":1,"strokeColor":"#1e1e1e","originalText":"Photosynthesis","autoResize":true},
  {"type":"text","id":"fo","x":245,"y":48,"text":"6CO2 + 6H2O --> C6H12O6 + 6O2","fontSize":16,"fontFamily":1,"strokeColor":"#757575","originalText":"6CO2 + 6H2O --> C6H12O6 + 6O2","autoResize":true},
  {"type":"rectangle","id":"lf","x":150,"y":90,"width":520,"height":380,"backgroundColor":"#d3f9d8","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#22c55e","strokeWidth":1,"opacity":35},
  {"type":"text","id":"lfl","x":170,"y":96,"text":"Inside the Leaf","fontSize":16,"fontFamily":1,"strokeColor":"#15803d","originalText":"Inside the Leaf","autoResize":true},

  {"type":"rectangle","id":"lr","x":190,"y":190,"width":160,"height":70,"backgroundColor":"#fff3bf","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#f59e0b","boundElements":[{"id":"t_lr","type":"text"},{"id":"a1","type":"arrow"},{"id":"a2","type":"arrow"},{"id":"a3","type":"arrow"},{"id":"a5","type":"arrow"}]},
  {"type":"text","id":"t_lr","x":195,"y":205,"width":150,"height":20,"text":"Light Reactions","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"lr","originalText":"Light Reactions","autoResize":true},

  {"type":"arrow","id":"a1","x":350,"y":225,"width":120,"height":0,"points":[[0,0],[120,0]],"strokeColor":"#1e1e1e","strokeWidth":2,"endArrowhead":"arrow","boundElements":[{"id":"t_a1","type":"text"}]},
  {"type":"text","id":"t_a1","x":390,"y":205,"width":40,"height":20,"text":"ATP","fontSize":14,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"a1","originalText":"ATP","autoResize":true},

  {"type":"rectangle","id":"cc","x":470,"y":190,"width":160,"height":70,"backgroundColor":"#d0bfff","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#8b5cf6","boundElements":[{"id":"t_cc","type":"text"},{"id":"a1","type":"arrow"},{"id":"a4","type":"arrow"},{"id":"a6","type":"arrow"}]},
  {"type":"text","id":"t_cc","x":475,"y":205,"width":150,"height":20,"text":"Calvin Cycle","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"cc","originalText":"Calvin Cycle","autoResize":true},

  {"type":"rectangle","id":"sl","x":10,"y":200,"width":120,"height":50,"backgroundColor":"#fff3bf","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#f59e0b","boundElements":[{"id":"t_sl","type":"text"},{"id":"a2","type":"arrow"}]},
  {"type":"text","id":"t_sl","x":15,"y":210,"width":110,"height":20,"text":"Sunlight","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"sl","originalText":"Sunlight","autoResize":true},

  {"type":"arrow","id":"a2","x":130,"y":225,"width":60,"height":0,"points":[[0,0],[60,0]],"strokeColor":"#f59e0b","strokeWidth":2,"endArrowhead":"arrow"},

  {"type":"rectangle","id":"wa","x":200,"y":360,"width":140,"height":50,"backgroundColor":"#a5d8ff","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#4a9eed","boundElements":[{"id":"t_wa","type":"text"},{"id":"a3","type":"arrow"}]},
  {"type":"text","id":"t_wa","x":205,"y":370,"width":130,"height":20,"text":"Water (H2O)","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"wa","originalText":"Water (H2O)","autoResize":true},

  {"type":"arrow","id":"a3","x":270,"y":360,"width":0,"height":-100,"points":[[0,0],[0,-100]],"strokeColor":"#4a9eed","strokeWidth":2,"endArrowhead":"arrow"},

  {"type":"rectangle","id":"co","x":480,"y":360,"width":130,"height":50,"backgroundColor":"#ffd8a8","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#f59e0b","boundElements":[{"id":"t_co","type":"text"},{"id":"a4","type":"arrow"}]},
  {"type":"text","id":"t_co","x":485,"y":370,"width":120,"height":20,"text":"CO2","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"co","originalText":"CO2","autoResize":true},

  {"type":"arrow","id":"a4","x":545,"y":360,"width":0,"height":-100,"points":[[0,0],[0,-100]],"strokeColor":"#f59e0b","strokeWidth":2,"endArrowhead":"arrow"},

  {"type":"rectangle","id":"ox","x":540,"y":100,"width":100,"height":40,"backgroundColor":"#ffc9c9","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#ef4444","boundElements":[{"id":"t_ox","type":"text"},{"id":"a5","type":"arrow"}]},
  {"type":"text","id":"t_ox","x":545,"y":105,"width":90,"height":20,"text":"O2","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"ox","originalText":"O2","autoResize":true},

  {"type":"arrow","id":"a5","x":310,"y":190,"width":230,"height":-50,"points":[[0,0],[230,-50]],"strokeColor":"#ef4444","strokeWidth":2,"endArrowhead":"arrow"},

  {"type":"rectangle","id":"gl","x":690,"y":195,"width":120,"height":60,"backgroundColor":"#c3fae8","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#22c55e","boundElements":[{"id":"t_gl","type":"text"},{"id":"a6","type":"arrow"}]},
  {"type":"text","id":"t_gl","x":695,"y":210,"width":110,"height":25,"text":"Glucose","fontSize":18,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"gl","originalText":"Glucose","autoResize":true},

  {"type":"arrow","id":"a6","x":630,"y":225,"width":60,"height":0,"points":[[0,0],[60,0]],"strokeColor":"#22c55e","strokeWidth":2,"endArrowhead":"arrow"},

  {"type":"ellipse","id":"sun","x":30,"y":110,"width":50,"height":50,"backgroundColor":"#fff3bf","fillStyle":"solid","strokeColor":"#f59e0b","strokeWidth":2},
  {"type":"arrow","id":"r1","x":55,"y":108,"width":0,"height":-14,"points":[[0,0],[0,-14]],"strokeColor":"#f59e0b","strokeWidth":2,"endArrowhead":null,"startArrowhead":null},
  {"type":"arrow","id":"r2","x":55,"y":162,"width":0,"height":14,"points":[[0,0],[0,14]],"strokeColor":"#f59e0b","strokeWidth":2,"endArrowhead":null,"startArrowhead":null},
  {"type":"arrow","id":"r3","x":28,"y":135,"width":-14,"height":0,"points":[[0,0],[-14,0]],"strokeColor":"#f59e0b","strokeWidth":2,"endArrowhead":null,"startArrowhead":null},
  {"type":"arrow","id":"r4","x":82,"y":135,"width":14,"height":0,"points":[[0,0],[14,0]],"strokeColor":"#f59e0b","strokeWidth":2,"endArrowhead":null,"startArrowhead":null}
]
```

---

## 示例 3：时序图（UML 风格）

演示一张包含参与者、虚线生命线和消息箭头的时序图。

```json
[
  {"type":"text","id":"title","x":200,"y":15,"text":"MCP Apps -- Sequence Flow","fontSize":24,"fontFamily":1,"strokeColor":"#1e1e1e","originalText":"MCP Apps -- Sequence Flow","autoResize":true},

  {"type":"rectangle","id":"uHead","x":60,"y":60,"width":100,"height":40,"backgroundColor":"#a5d8ff","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#4a9eed","strokeWidth":2,"boundElements":[{"id":"t_uHead","type":"text"}]},
  {"type":"text","id":"t_uHead","x":65,"y":65,"width":90,"height":20,"text":"User","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"uHead","originalText":"User","autoResize":true},

  {"type":"arrow","id":"uLine","x":110,"y":100,"width":0,"height":400,"points":[[0,0],[0,400]],"strokeColor":"#b0b0b0","strokeWidth":1,"strokeStyle":"dashed","endArrowhead":null},

  {"type":"rectangle","id":"aHead","x":230,"y":60,"width":100,"height":40,"backgroundColor":"#d0bfff","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#8b5cf6","strokeWidth":2,"boundElements":[{"id":"t_aHead","type":"text"}]},
  {"type":"text","id":"t_aHead","x":235,"y":65,"width":90,"height":20,"text":"Agent","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"aHead","originalText":"Agent","autoResize":true},

  {"type":"arrow","id":"aLine","x":280,"y":100,"width":0,"height":400,"points":[[0,0],[0,400]],"strokeColor":"#b0b0b0","strokeWidth":1,"strokeStyle":"dashed","endArrowhead":null},

  {"type":"rectangle","id":"sHead","x":420,"y":60,"width":130,"height":40,"backgroundColor":"#ffd8a8","fillStyle":"solid","roundness":{"type":3},"strokeColor":"#f59e0b","strokeWidth":2,"boundElements":[{"id":"t_sHead","type":"text"}]},
  {"type":"text","id":"t_sHead","x":425,"y":65,"width":120,"height":20,"text":"Server","fontSize":16,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"sHead","originalText":"Server","autoResize":true},

  {"type":"arrow","id":"sLine","x":485,"y":100,"width":0,"height":400,"points":[[0,0],[0,400]],"strokeColor":"#b0b0b0","strokeWidth":1,"strokeStyle":"dashed","endArrowhead":null},

  {"type":"arrow","id":"m1","x":110,"y":150,"width":170,"height":0,"points":[[0,0],[170,0]],"strokeColor":"#1e1e1e","strokeWidth":2,"endArrowhead":"arrow","boundElements":[{"id":"t_m1","type":"text"}]},
  {"type":"text","id":"t_m1","x":165,"y":130,"width":60,"height":20,"text":"request","fontSize":14,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"m1","originalText":"request","autoResize":true},

  {"type":"arrow","id":"m2","x":280,"y":200,"width":205,"height":0,"points":[[0,0],[205,0]],"strokeColor":"#8b5cf6","strokeWidth":2,"endArrowhead":"arrow","boundElements":[{"id":"t_m2","type":"text"}]},
  {"type":"text","id":"t_m2","x":352,"y":180,"width":60,"height":20,"text":"tools/call","fontSize":14,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"m2","originalText":"tools/call","autoResize":true},

  {"type":"arrow","id":"m3","x":485,"y":260,"width":-205,"height":0,"points":[[0,0],[-205,0]],"strokeColor":"#f59e0b","strokeWidth":2,"endArrowhead":"arrow","strokeStyle":"dashed","boundElements":[{"id":"t_m3","type":"text"}]},
  {"type":"text","id":"t_m3","x":352,"y":240,"width":60,"height":20,"text":"result","fontSize":14,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"m3","originalText":"result","autoResize":true},

  {"type":"arrow","id":"m4","x":280,"y":320,"width":-170,"height":0,"points":[[0,0],[-170,0]],"strokeColor":"#8b5cf6","strokeWidth":2,"endArrowhead":"arrow","strokeStyle":"dashed","boundElements":[{"id":"t_m4","type":"text"}]},
  {"type":"text","id":"t_m4","x":165,"y":300,"width":60,"height":20,"text":"response","fontSize":14,"fontFamily":1,"strokeColor":"#1e1e1e","textAlign":"center","verticalAlign":"middle","containerId":"m4","originalText":"response","autoResize":true}
]
```

---

## 需要避免的常见错误

- **不要使用 `"label"` 属性**——这是头号错误。它不是 Excalidraw 文件格式的一部分，会被静默忽略，从而产生没有可见文本的空白形状。始终使用容器绑定（`containerId` + `boundElements`），如上面的示例所示。
- **每个绑定文本都需要双向链接**——形状需要 `boundElements: [{"id": "t_xxx", "type": "text"}]`，而文本需要 `containerId: "shape_id"`。缺少任何一方，绑定都不会生效。
- **在所有文本元素上包含 `originalText` 和 `autoResize: true`**——Excalidraw 用它们来实现正确的文本重排。
- **在所有文本元素上包含 `fontFamily: 1`**——缺少它，文本可能无法以预期的手绘字体渲染。
- **当 y 坐标接近时元素会重叠**——始终检查文本、方框和标签不会堆叠在一起
- **箭头标签需要空间**——像 "ATP + NADPH" 这样长的标签会溢出短箭头。保持标签简短，或者把箭头加宽
- **标题要相对于图表居中**——估算总宽度，并将标题文本居中放置于其上
- **最后再绘制装饰**——可爱的插图（太阳、星星、图标）应放在数组的末尾，这样它们会被绘制在最上层

