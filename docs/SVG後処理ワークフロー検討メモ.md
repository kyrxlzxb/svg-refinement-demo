# SVG後処理ワークフロー検討メモ

## 1. 背景・目的

道路標識画像 → SVG生成用に、PNG画像と最終SVGを使用して3BクラスVLMをFine-tuning中。現在の出力は全体として元画像に近く、Text、Shield、Arrow等の必要要素もほぼ生成できている。

残課題は主にSVG特有の幾何ずれ。学習データ追加だけでは完全解消しにくいため、**VLM出力後にMultimodal LLM + PythonによるSVG Refinementを追加する**。

目的はSVG再生成ではなく、**既に正しい要素を保持し、問題部分だけ修正すること**。

---

## 2. 全体構成

```text
Reference PNG + Generated SVG
        ↓
SVG Parse / Normalize
        ↓
SVG Manifest + Current Render
        ↓
Multimodal LLM
  ・Semantic理解
  ・問題検出
  ・Anchor選択
  ・Repair Plan
        ↓
Python SVG Tools
  ・数値計算
  ・SVG変更
        ↓
Render
        ↓
LLM再確認
        ↓
必要ならRepeat
```

Multi-Agentは使用せず、**Single Orchestrator + LLM + deterministic tools** とする。1画像ずつ処理するため複雑なAgent間通信は不要。

---

## 3. LLM / Codeの役割

| LLM | Python |
|---|---|
| ReferenceとRender比較 | SVG parsing |
| Object分類・Grouping | Path normalization |
| Anchor判定 | BBox / Center計算 |
| 問題箇所判定 | Centerline計算 |
| 修正Operation選択 | Translation / Scaling |
| Arrow Landmark識別 | Path node変更 |
| OCR修正内容判定 | Z-order変更 |
| 修正後Validation | Render / Save |

基本ルール：

> **LLM = 何が問題か / 何を直すか**  
> **Code = どの座標をどれだけ変更するか**

LLMに最終座標やTransform Matrix値を直接決めさせない。

---

## 4. SVG Manifest

SVGをそのままLLMへ渡すのではなく、解析後にStable IDを付ける。

```text
path_001, path_002, text_001, rect_001 ...
```

共通情報：`id / type / parent / z-order / fill / stroke / transform / bbox / center`

Text：`content / font / font-size / transform / bbox`

Path：`absolute commands / segments / endpoints / control points / bbox / center`

---

## 5. Path Normalization

ArrowはPath開始位置が一定ではなく、余分な点が入る場合もある。そのため、

```text
n番目のPoint = 特定のArrow部位
```

という前提は禁止。

Normalize時に①Relative→Absolute変換、②EndpointとControl Pointを分離、③Segment順序保持、④NodeにStable ID付与。

```text
path_017.node_00
path_017.node_01
...
```

その後、LLMがNodeとSemantic Landmarkを対応付けする。

---

## 6. 修正対象と制約

| 対象 | 主な問題 | 許可する修正 |
|---|---|---|
| Text | OCR、幅、位置、Canvas外 | Text変更、Transform、移動 |
| Shield | Layer中心ずれ | Translationのみ |
| Border | Layer中心ずれ | Translationのみ |
| Panel | Z-order | Element reorder |
| Arrow | Head/Shaft不整合 | 局所Path修正可 |
| Arrow Group | 共通Baseずれ | Translation |

**Arrow以外のPath Geometryは変更禁止。** Shield、Border、Panel等は形状・サイズを保持する。

---

## 7. Shield / Border Alignment

複数Pathを重ねて作るShieldやBorderで、個々の形状は正しいがCenterがずれるケースがある。

処理：①LLMが同一ObjectのPath群をGrouping、②Referenceと最も一致するPathをAnchor選択、③各BBox Center計算、④他PathをTranslationしてCenter一致。

Outer PathをAnchor候補にできるが固定ルールにはしない。

---

## 8. Z-order

例：Green IC PanelがWhite Textより後に描画され、Textを隠す。

LLMがSemantic Objectと前後関係を判断し、PythonはElement Orderのみ変更する。

基本：

```text
Background → Border / Panel → Inner Shape → Symbol / Text
```

Geometryは変更しない。

---

## 9. Text

### OCR
Reference PNGとSVG Textを比較し、誤字のみ置換。文字変更後にWidthが変わるため、Layout調整は後段。

### Width / Position
日本語Fontと英語Fontはほぼ固定。Pixel単位の完全一致は不要。

条件：①Referenceと大体同位置、②他要素と重ならない、③Panel内に収まる、④Canvas外に出ない。

LLMは「`text_08`が広すぎる」「`panel_03`内へ収める」までを判断。実際のHorizontal ScaleやTranslationはPythonで計算する。

---

## 10. Arrowの問題

VLMは局所的なCurveやShaft形状は学習できているが、Arrow全体の幾何制約を安定して維持できない。

典型例：

```text
Head形状 ≒ 正しい
Shaft Width ≒ 正しい
↓
Head中心 ≠ Shaft Centerline
↓
片側のHead-Shaft接続が合わない
↓
最後に離れたPoint同士を直線接続
↓
Arrow片側が欠けた形になる
```

この部分のみPath Nodeの局所修正を許可する。

---

## 11. Arrow Landmark

Normalize後、LLMが必要なLandmarkをNodeに対応付けする。

例：

`Apex / Left Shoulder / Right Shoulder / Shaft Left Boundary / Shaft Right Boundary / Left Junction / Right Junction / Bottom Left / Bottom Right`

固定Node番号は使用しない。

---

## 12. 直進Arrow

処理：①Shaft左右Boundary特定、②Centerline計算、③Apex / Shoulder / Junction特定、④Head中心とCenterline比較、⑤崩れている側を判定、⑥Head/Junctionのみ局所修正。

保持：`Shaft Width / 全体サイズ / 全体位置 / 正常なCurve`

片側のみ正常な場合は、正常側をCenterline基準でMirrorして反対側を修復可能。

---

## 13. Turn Arrow

例：

```text
    │
    │
    └────►
```

Vertical ShaftとHorizontal ShaftのWidthは一致するとは限らない。

そのためArrow全体で1本のCenterlineを使わず、**Headに直接接続するShaftのCenterlineを基準にする**。

右向きHead → Horizontal ShaftのUpper/Lower BoundaryからCenterline計算。左向きも同様。

---

## 14. Multi-Arrow Base Alignment

3方向Arrow等では複数Pathの下側Vertical Stemが本来重なる。

処理：①同一Arrow GroupをGrouping、②共通Base/Stem特定、③Referenceと最も一致するPathをAnchor、④他PathをTranslation、⑤BaseをOverlap。

Scale変更は原則行わない。

---

## 15. Anchor方式

Referenceと高い一致度を持つElementをAnchorとして利用する。

例：

- 正しいOuter Shield
- 正しいBorder
- 正しいPanel
- 正しいArrow Shaft
- 正しいText

Absolute座標をLLMに推定させるより、

```text
Inner Shield → Outer Shield Center
Arrow Head → Shaft Centerline
Text → Panel内部
```

のようなRelative修正を優先する。

---

## 16. 修正順序

```text
1. Parse / Normalize
2. Semantic Object + Anchor識別
3. Text OCR
4. Z-order
5. Shield / Border / Panel Alignment
6. Arrow Internal Geometry
7. Arrow Group Alignment
8. Text Width / Position
9. Collision / Canvas Check
10. Final Validation
```

固定一回処理ではなく、各重要修正後に `Render → Re-analyze` を行う。

---

## 17. Structured Output

LLMにSVGコードを書かせず、Repair ActionをStructured Outputで返す。

```json
{
  "issue_type": "ALIGN_COMPOSITE_CENTER",
  "targets": ["path_012", "path_013"],
  "anchor": "path_012",
  "confidence": 0.95
}
```

Arrow：

```json
{
  "issue_type": "REPAIR_ARROW_INTERNAL",
  "target": "path_020",
  "landmarks": {
    "apex": "node_04",
    "shaft_left": "node_11",
    "shaft_right": "node_18"
  },
  "defective_side": "right"
}
```

最終数値はPython側で計算。

---

## 18. Iterative Refinement

修正により別問題が発生する可能性があるため、

```text
Analyze → Repair → Render → Re-analyze
```

を繰り返す。

例：Arrow位置修正 → TextとのOverlap発生 → 次IterationでText修正。

Demoでは数回のIteration Limitを設定し、Convergence処理は作らない。

---

## 19. Demo実装順

まず：

```text
PNG + SVG
→ Parse
→ Render
→ Manifest
→ Bedrock
→ Structured Issues
→ Python Repair
→ Render
```

を確認。

初期Operation：

`ALIGN_COMPOSITE_CENTER / REORDER_ELEMENTS / REPLACE_TEXT / FIT_TEXT_WIDTH`

基本Loop確認後にArrowを追加する。

Arrowは `Path Normalize → Landmark → Internal Repair → Group Alignment → Validation` の順で実装。

---

## 20. 最初に確認する項目

1. ManifestからLLMがElementを正しく対応付けできるか  
2. Reference + RenderからAnchorを選べるか  
3. Shield等を正しくGroupingできるか  
4. Structured Repair Actionを安定して返せるか  
5. Arrow Normalize後にLandmarkを認識できるか  
6. Centerline基準修正でArrowが改善するか  
7. Repair → Render → Re-evaluateが実用的に回るか  

この7点が確認できれば、現在の後処理方式は成立すると判断する。