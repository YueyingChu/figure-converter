/* Runs only against a temporary source copy. No changes are saved to user documents. */
(function () {
    var c = FC_CONFIG;
    var result = {success:false, warnings:[], artboards:[], preprocessing:{}};
    var doc = null;
    var previous = app.documents.length ? app.activeDocument : null;
    var interaction = app.userInteractionLevel;
    function encode(value) {
        if (value === null) return "null";
        if (typeof value === "string") return '"' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\r/g, "\\r").replace(/\n/g, "\\n").replace(/\t/g, "\\t") + '"';
        if (typeof value === "number" || typeof value === "boolean") return String(value);
        var parts = [], key;
        if (value instanceof Array) {
            for (key = 0; key < value.length; key++) parts.push(encode(value[key]));
            return "[" + parts.join(",") + "]";
        }
        for (key in value) if (value.hasOwnProperty(key)) parts.push(encode(key) + ":" + encode(value[key]));
        return "{" + parts.join(",") + "}";
    }
    function visible(item) {
        var p = item;
        while (p && p.typename !== "Document" && p.typename !== "Application") {
            if (p.opacity === 0) return false;
            if (p.typename === "Layer") { if (!p.visible) return false; }
            else if (p.hidden) return false;
            p = p.parent;
        }
        return true;
    }
    function unlockLayers(layers) {
        for (var j = 0; j < layers.length; j++) {
            if (layers[j].visible) {
                layers[j].locked = false;
                unlockLayers(layers[j].layers);
            }
        }
    }
    function intersect(a, b) {
        if (!a || !b) return null;
        var r = [Math.max(a[0], b[0]), Math.min(a[1], b[1]), Math.min(a[2], b[2]), Math.max(a[3], b[3])];
        return r[2] > r[0] && r[1] > r[3] ? r : null;
    }
    function union(a, b) {
        if (!a) return b;
        if (!b) return a;
        return [Math.min(a[0],b[0]), Math.max(a[1],b[1]), Math.max(a[2],b[2]), Math.min(a[3],b[3])];
    }
    function isClip(item) {
        if (item.typename === "PathItem") return item.clipping;
        if (item.typename === "CompoundPathItem") {
            for (var j=0; j<item.pathItems.length; j++) if (item.pathItems[j].clipping) return true;
        }
        return false;
    }
    function hasPaint(item) {
        if (item.typename === "PathItem") return !item.guides && (item.filled || item.stroked);
        if (item.typename === "CompoundPathItem") {
            for (var k=0; k<item.pathItems.length; k++) if (hasPaint(item.pathItems[k])) return true;
            return false;
        }
        return true;
    }
    function artworkBounds(item, clip) {
        if (!visible(item) || item.opacity === 0) return null;
        if (item.typename === "GroupItem") {
            var groupClip = clip, combined = null, j;
            if (item.clipped) {
                for (j=0; j<item.pageItems.length; j++) {
                    if (item.pageItems[j].parent === item && isClip(item.pageItems[j])) {
                        var mask = item.pageItems[j];
                        groupClip = intersect(groupClip, mask.geometricBounds);
                        // Masks can have a visible fill or stroke of their own.
                        // That paint is constrained only by ancestor masks.
                        if (visible(mask) && hasPaint(mask)) combined = union(combined, intersect(mask.visibleBounds, clip));
                        break;
                    }
                }
                if (!groupClip) return combined;
            }
            for (j=0; j<item.pageItems.length; j++) {
                if (item.pageItems[j].parent === item) combined = union(combined, artworkBounds(item.pageItems[j], groupClip));
            }
            return combined;
        }
        if (isClip(item)) return null;
        if (!hasPaint(item)) return null;
        try { return intersect(item.visibleBounds, clip); } catch (e) { return null; }
    }
    function cropBounds(original) {
        var combined = null;
        for (var j=0; j<doc.pageItems.length; j++) {
            var item = doc.pageItems[j];
            if (item.parent.typename === "Layer") combined = union(combined, artworkBounds(item, original));
        }
        return combined;
    }
    function preservePlacedAppearance(item) {
        // Some AI files have placed artwork with cached pixels but no
        // accessible source File. Illustrator can display/capture it, while
        // SVG silently omits it. Rasterize this object alone, never the page.
        var bounds = item.visibleBounds, parent = item.parent;
        while (parent && parent.typename !== "Document") {
            if (parent.typename === "GroupItem" && parent.clipped) {
                for (var m=0; m<parent.pageItems.length; m++) {
                    if (parent.pageItems[m].parent === parent && isClip(parent.pageItems[m])) {
                        bounds = intersect(bounds, parent.pageItems[m].geometricBounds);
                        break;
                    }
                }
            }
            parent = parent.parent;
        }
        var onBoards = null, boardCount = c.firstArtboard ? 1 : originalBoards.length;
        for (var a=0; a<boardCount; a++) onBoards = union(onBoards, intersect(bounds, originalBoards[a]));
        if (!onBoards) { item.hidden = true; return 0; }
        bounds = onBoards;
        var resolution = Math.max(600, c.dpi);
        if (c.widthMm) {
            for (var b=0; b<originalBoards.length; b++) {
                if (!intersect(bounds, originalBoards[b])) continue;
                var fitted = c.trimArtboard ? cropBounds(originalBoards[b]) : originalBoards[b];
                if (fitted) resolution = Math.max(resolution, c.dpi * c.widthMm / 25.4 * 72 / (fitted[2]-fitted[0]));
            }
        }
        if (resolution > 2400) {
            resolution = 2400;
            result.warnings.push("部分置入图片只能按文档中保存的内容恢复；放大后请检查这些图片的清晰度。");
        }
        if ((bounds[2]-bounds[0]) * (bounds[1]-bounds[3]) * Math.pow(resolution/72,2) > 100000000) {
            throw new Error("一张置入图片过大，无法安全恢复。请先在 Illustrator 中重新嵌入该图片。");
        }
        var wrapper = item.parent.groupItems.add();
        wrapper.move(item, ElementPlacement.PLACEBEFORE);
        item.move(wrapper, ElementPlacement.PLACEATBEGINNING);
        var options = new RasterizeOptions();
        options.resolution = resolution;
        options.transparency = true;
        options.antiAliasingMethod = AntiAliasingMethod.ARTOPTIMIZED;
        var raster = doc.rasterize(item, bounds, options);
        raster.move(wrapper, ElementPlacement.PLACEATBEGINNING);
        return resolution;
    }
    try {
        app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
        doc = app.open(new File(c.input));
        var originalBoards = [];
        for (var bi=0; bi<doc.artboards.length; bi++) {
            var br=doc.artboards[bi].artboardRect;
            originalBoards.push([br[0],br[1],br[2],br[3]]);
        }
        var folder = new Folder(c.output);
        folder.create();
        unlockLayers(doc.layers);
        var i;
        for (i = 0; i < doc.pageItems.length; i++) {
            if (visible(doc.pageItems[i])) try { doc.pageItems[i].locked = false; } catch (e) {}
        }
        // Embed resolved links so exported SVG remains self-contained.
        var embedded = 0, unlinkedPlaced = 0, recoveredPlaced = [];
        for (i = doc.placedItems.length - 1; i >= 0; i--) {
            var placed = doc.placedItems[i];
            if (!visible(placed)) continue;
            var linked = null;
            try { linked = placed.file; } catch (e) {
                // Illustrator can retain placed artwork internally with no
                // external File association. Embed its cached contents so
                // SVG export does not silently omit the placed artwork.
                if (String(e.message || e).indexOf("no file associated") < 0) throw e;
            }
            if (!linked) {
                try { placed.embed(); }
                catch (embedError) {
                    var recoveredDpi = preservePlacedAppearance(placed);
                    if (recoveredDpi) recoveredPlaced.push(recoveredDpi);
                }
                unlinkedPlaced++;
                continue;
            }
            if (!linked.exists) {
                throw new Error("存在找不到的链接图像。请先在 Illustrator 中嵌入链接图像后重试。");
            }
            placed.embed();
            embedded++;
        }
        result.preprocessing.embedded_links = embedded;
        result.preprocessing.placed_items_without_external_file = unlinkedPlaced;
        result.preprocessing.recovered_placed_image_dpi = recoveredPlaced;
        if (recoveredPlaced.length) result.warnings.push("已恢复 " + recoveredPlaced.length + " 个无法直接嵌入的图像对象，按清晰图片保留；其余文字和线条仍按矢量处理。");
        result.preprocessing.embedded_raster_items = doc.rasterItems.length;
        var outlined = 0;
        if (c.outlineText) {
            for (i = doc.textFrames.length - 1; i >= 0; i--) {
                var text = doc.textFrames[i];
                if (visible(text)) { text.createOutline(); outlined++; }
            }
        }
        result.preprocessing.outlined_text_frames = outlined;
        var strokes = 0;
        if (c.outlineStrokes) {
            doc.selection = null;
            for (i = 0; i < doc.pathItems.length; i++) {
                var path = doc.pathItems[i];
                if (visible(path) && path.stroked && !path.guides && !path.clipping) {
                    path.selected = true;
                    strokes++;
                }
            }
            if (strokes) {
                app.executeMenuCommand("Live Outline Stroke");
                app.executeMenuCommand("expandStyle");
            }
            doc.selection = null;
        }
        result.preprocessing.stroke_paths_selected = strokes;
        var remainingStrokes = 0;
        for (i = 0; i < doc.pathItems.length; i++) {
            if (visible(doc.pathItems[i]) && doc.pathItems[i].stroked && !doc.pathItems[i].guides) remainingStrokes++;
        }
        result.preprocessing.remaining_stroked_paths = remainingStrokes;
        if (c.outlineStrokes && remainingStrokes) result.warnings.push("有 " + remainingStrokes + " 条线未能转为轮廓，已按原样保留。请检查这些线条的粗细和虚线间隔。");
        result.totalArtboards = doc.artboards.length;
        // Preserve native artwork before artboard-specific export changes. This is
        // a new private file; the user's source is never opened or saved here.
        if (c.documentSource) {
            try {
                var checkpointOptions = new IllustratorSaveOptions();
                checkpointOptions.pdfCompatible = false;
                checkpointOptions.compressed = true;
                doc.saveAs(new File(c.documentSource), checkpointOptions);
                result.documentSource = c.documentSource;
            } catch (checkpointError) {
                result.documentError = "PDF / EPS 临时文档准备失败：" + String(checkpointError.message || checkpointError);
            }
        }
        var limit = c.firstArtboard ? 1 : doc.artboards.length;
        for (i = 0; i < limit; i++) {
            doc.artboards.setActiveArtboardIndex(i);
            var board = doc.artboards[i];
            var original = originalBoards[i], rect = original;
            if (c.trimArtboard) {
                var fitted = cropBounds(original);
                if (fitted && fitted[2]-fitted[0] >= 0.01 && fitted[1]-fitted[3] >= 0.01) rect = fitted;
                else result.warnings.push("画板 " + (i+1) + " 没有找到可见图形，已保留原画板大小。");
            }
            board.artboardRect = rect;
            var sourceWidth = rect[2]-rect[0], sourceHeight = rect[1]-rect[3];
            var scale = c.widthMm ? (c.widthMm / 25.4 * 72) / sourceWidth : 1;
            var entry = {index:i + 1, name:board.name, width_pt:sourceWidth*scale, height_pt:sourceHeight*scale,
                source_width_pt:sourceWidth, source_height_pt:sourceHeight,
                original_width_pt:original[2]-original[0], original_height_pt:original[1]-original[3],
                crop_bounds:rect, trimmed:Math.abs(sourceWidth-(original[2]-original[0]))>0.001 || Math.abs(sourceHeight-(original[1]-original[3]))>0.001};
            result.artboards.push(entry);
            var boardFolder = new Folder(folder.fsName + "/artboard-" + (i+1));
            boardFolder.create();
            if (c.png) {
                var w = Math.ceil(entry.width_pt / 72 * c.dpi), h = Math.ceil(entry.height_pt / 72 * c.dpi);
                if (w * h > 100000000 || w > 30000 || h > 30000) throw new Error("画板 " + (i+1) + " 的 PNG 超过安全尺寸，请降低 DPI 或缩小画板。");
                var png = new File(boardFolder.fsName + "/figure.png");
                var white = new RGBColor(); white.red = 255; white.green = 255; white.blue = 255;
                var renderDpi = c.dpi * scale;
                if (renderDpi >= 72 && renderDpi <= 2400) {
                    var capture = new ImageCaptureOptions();
                    capture.resolution = renderDpi;
                    capture.antiAliasing = true;
                    capture.transparency = c.transparent;
                    capture.matte = !c.transparent;
                    capture.matteColor = white;
                    doc.imageCapture(png, rect, capture);
                } else {
                    // PNG24's scale supports output sizes outside imageCapture's DPI range.
                    var pngOptions = new ExportOptionsPNG24();
                    pngOptions.horizontalScale = renderDpi / 72 * 100;
                    pngOptions.verticalScale = pngOptions.horizontalScale;
                    pngOptions.artBoardClipping = true;
                    pngOptions.antiAliasing = true;
                    pngOptions.transparency = c.transparent;
                    pngOptions.matte = !c.transparent;
                    pngOptions.matteColor = white;
                    doc.exportFile(png, ExportType.PNG24, pngOptions);
                }
                if (!png.exists || !png.length) throw new Error("PNG 导出未生成文件。");
                entry.png = png.fsName;
            }
            if (c.svg) {
                var options = new ExportOptionsSVG();
                options.coordinatePrecision = 6;
                options.embedRasterImages = true;
                options.fontType = c.outlineText ? SVGFontType.OUTLINEFONT : SVGFontType.SVGFONT;
                options.cssProperties = SVGCSSPropertyLocation.PRESENTATIONATTRIBUTES;
                options.saveMultipleArtboards = true;
                options.artboardRange = String(i + 1);
                options.preserveEditability = false;
                doc.exportFile(new File(boardFolder.fsName + "/figure.svg"), ExportType.SVG, options);
                var svgs = boardFolder.getFiles("*.svg");
                if (svgs.length !== 1 || !svgs[0].length) throw new Error("SVG 画板导出结果不完整。");
                entry.svg = svgs[0].fsName;
            }
        }
        result.success = true;
    } catch (error) {
        result.error = String(error.message || error) + (error.line ? " (JSX line " + error.line + ")" : "");
    } finally {
        if (doc) { try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (closeError) { result.warnings.push("临时文档未能自动关闭，请在 Illustrator 中关闭它，无需保存。"); } }
        if (previous) { try { app.activeDocument = previous; } catch (restoreError) {} }
        app.userInteractionLevel = interaction;
        var file = new File(c.result);
        file.encoding = "UTF-8";
        if (file.open("w")) { file.write(encode(result)); file.close(); }
    }
    return encode(result);
})();
