/* Native PDF/EPS export from a private, already preprocessed AI checkpoint.
 * Inject documentConfig = {source, result, entries:[{index,bounds,width_pt,
 * height_pt,pdf?,eps?}]} before this script. Never saves the checkpoint.
 */
(function () {
    var c = documentConfig;
    var result = {success:false, outputs:[], errors:[], warnings:[]};
    var doc = null;
    var previous = app.documents.length ? app.activeDocument : null;
    var interaction = app.userInteractionLevel;
    var coordinates = app.coordinateSystem;
    function encode(value) {
        if (value === null) return "null";
        if (typeof value === "string") return '"' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/[\u0000-\u001f]/g, function (ch) {
            var hex = ch.charCodeAt(0).toString(16);
            return "\\u" + ("0000" + hex).slice(-4);
        }) + '"';
        if (typeof value === "number" || typeof value === "boolean") return String(value);
        var parts = [], key;
        if (value instanceof Array) {
            for (key=0; key<value.length; key++) parts.push(encode(value[key]));
            return "[" + parts.join(",") + "]";
        }
        for (key in value) if (value.hasOwnProperty(key)) parts.push(encode(key) + ":" + encode(value[key]));
        return "{" + parts.join(",") + "}";
    }
    function intersect(a, b) {
        if (!a || !b) return null;
        var r = [Math.max(a[0],b[0]),Math.min(a[1],b[1]),Math.min(a[2],b[2]),Math.max(a[3],b[3])];
        return r[2] > r[0] && r[1] > r[3] ? r : null;
    }
    function union(a, b) {
        if (!a) return b;
        if (!b) return a;
        return [Math.min(a[0],b[0]),Math.max(a[1],b[1]),Math.max(a[2],b[2]),Math.min(a[3],b[3])];
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
    function isClip(item) {
        if (item.typename === "PathItem") return item.clipping;
        if (item.typename === "CompoundPathItem") {
            for (var j=0; j<item.pathItems.length; j++) if (item.pathItems[j].clipping) return true;
        }
        return false;
    }
    function painted(item) {
        if (item.typename === "PathItem") return !item.guides && (item.filled || item.stroked);
        if (item.typename === "CompoundPathItem") {
            for (var j=0; j<item.pathItems.length; j++) if (painted(item.pathItems[j])) return true;
            return false;
        }
        return true;
    }
    function boundsWithin(item, region) {
        if (!visible(item)) return null;
        if (item.typename === "GroupItem") {
            var clipping = region, combined = null, j;
            if (item.clipped) {
                for (j=0; j<item.pageItems.length; j++) {
                    var mask = item.pageItems[j];
                    if (mask.parent === item && isClip(mask)) {
                        clipping = intersect(region, mask.geometricBounds);
                        if (visible(mask) && painted(mask)) combined = union(combined, intersect(mask.visibleBounds, region));
                        break;
                    }
                }
                if (!clipping) return combined;
            }
            for (j=0; j<item.pageItems.length; j++) {
                if (item.pageItems[j].parent === item) combined = union(combined, boundsWithin(item.pageItems[j], clipping));
            }
            return combined;
        }
        if (isClip(item) || !painted(item)) return null;
        return intersect(item.visibleBounds, region);
    }
    function unlockLayers(layers) {
        for (var j=0; j<layers.length; j++) {
            layers[j].locked = false;
            unlockLayers(layers[j].layers);
        }
    }
    function prepare(entry) {
        var r = entry.bounds, width = entry.width_pt, height = entry.height_pt;
        if (!r || r.length !== 4 || !(width > 0) || !(height > 0) || !isFinite(width) || !isFinite(height)) throw new Error("画板尺寸无效。");
        for (var k=0; k<4; k++) if (!isFinite(r[k])) throw new Error("画板边界无效。");
        var originalWidth = r[2]-r[0], originalHeight = r[1]-r[3];
        if (!(originalWidth > 0) || !(originalHeight > 0)) throw new Error("画板边界无效。");
        var scale = width/originalWidth;
        if (Math.abs(height/originalHeight-scale) > Math.max(1,scale)*0.00001) throw new Error("导出尺寸必须保持原图长宽比。");
        if (Math.max(width,height) > 16300) throw new Error("PDF/EPS 尺寸过大，请减小导出宽度。");
        if (entry.index < 1 || entry.index > doc.artboards.length || entry.index !== Math.floor(entry.index)) throw new Error("画板编号无效。");
        doc.artboards.setActiveArtboardIndex(entry.index-1);
        doc.selection = null;
        unlockLayers(doc.layers);
        var j, top = [];
        for (j=0; j<doc.pageItems.length; j++) {
            try { doc.pageItems[j].locked = false; } catch (ignored) {}
            if (doc.pageItems[j].parent.typename === "Layer") top.push(doc.pageItems[j]);
        }
        // Remove artwork belonging only to other artboards before scaling.
        // Keep intersecting groups intact so layer opacity and nested masks
        // retain their native rendering; the save operation clips the page.
        for (j=top.length-1; j>=0; j--) {
            // Hidden ancestors cannot be modified in Illustrator. Leave them
            // hidden and out of the transform list; native output omits them.
            if (!visible(top[j])) { top.splice(j,1); continue; }
            if (!boundsWithin(top[j],r)) { top[j].remove(); top.splice(j,1); }
        }
        var matrix = app.getScaleMatrix(scale*100,scale*100);
        for (j=0; j<top.length; j++) {
            var before = top[j].position;
            var wantedX = (before[0]-r[0])*scale;
            var wantedY = (before[1]-r[3])*scale;
            // A local anchor avoids scaling a distant artboard's absolute
            // coordinates outside Illustrator's canvas before translating.
            // Unlike getScaleMatrix's percentage arguments, transform's
            // line-width argument is a multiplier in Illustrator. Passing
            // scale*100 would turn a 4 pt stroke into a 1000 pt filled border.
            top[j].transform(matrix,true,true,true,true,scale,Transformation.TOPLEFT);
            var after = top[j].position;
            top[j].translate(wantedX-after[0],wantedY-after[1],true,true,true,true);
        }
        for (j=doc.artboards.length-1; j>=0; j--) if (j !== entry.index-1) doc.artboards.remove(j);
        doc.artboards.setActiveArtboardIndex(0);
        doc.artboards[0].artboardRect = [0,height,width,0];
        doc.artboards[0].name = "FC_BOARD";
        doc.selection = null;
        app.redraw();
    }
    function savePDF(destination) {
        var options = new PDFSaveOptions();
        options.compatibility = PDFCompatibility.ACROBAT7;
        options.preserveEditability = false;
        options.artboardRange = "1";
        options.acrobatLayers = false;
        options.generateThumbnails = false;
        options.viewAfterSaving = false;
        options.compressArt = true;
        options.colorDownsampling = 0;
        options.grayscaleDownsampling = 0;
        options.monochromeDownsampling = 0;
        options.colorCompression = CompressionQuality.ZIP8BIT;
        options.grayscaleCompression = CompressionQuality.ZIP8BIT;
        options.colorConversionID = ColorConversion.None;
        options.bleedLink = true;
        options.bleedOffsetRect = [0,0,0,0];
        options.trimMarks = false;
        options.registrationMarks = false;
        options.colorBars = false;
        options.pageInformation = false;
        doc.saveAs(destination,options);
    }
    function saveEPS(destination) {
        // Illustrator additionally writes an all-artwork EPS when exporting
        // artboards. Stage both files privately and publish only the explicitly
        // clipped artboard file, never that aggregate companion.
        var stage = new Folder(destination.parent.fsName + "/eps-" + new Date().getTime() + "-" + Math.floor(Math.random()*100000000));
        if (!stage.create()) throw new Error("无法创建 EPS 临时文件夹。");
        var aggregate = new File(stage.fsName + "/artwork.eps");
        var options = new EPSSaveOptions();
        options.embedAllFonts = true;
        options.embedLinkedFiles = true;
        options.includeDocumentThumbnails = false;
        options.preview = EPSPreview.None;
        options.postScript = EPSPostScriptLevelEnum.LEVEL3;
        options.compatibleGradientPrinting = false;
        options.flattenOutput = OutputFlattening.PRESERVEAPPEARANCE;
        options.cmykPostScript = doc.documentColorSpace === DocumentColorSpace.CMYK;
        options.saveMultipleArtboards = true;
        options.artboardRange = "1";
        doc.saveAs(aggregate,options);
        var files = stage.getFiles("*.eps"), candidate = null;
        for (var j=0; j<files.length; j++) {
            if (files[j].fsName !== aggregate.fsName) {
                if (candidate) throw new Error("EPS 返回了多个画板文件，已停止以避免导出错误画板。");
                candidate = files[j];
            }
        }
        if (!candidate) throw new Error("Illustrator 未生成指定画板的 EPS 文件。");
        if (!candidate.copy(destination.fsName)) throw new Error("无法保存 EPS 输出文件。");
    }
    try {
        app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
        app.coordinateSystem = CoordinateSystem.DOCUMENTCOORDINATESYSTEM;
        var source = new File(c.source);
        if (!source.exists) throw new Error("找不到预处理后的临时 AI 文件。");
        for (var warningIndex=0; warningIndex<c.entries.length; warningIndex++) {
            if (c.entries[warningIndex].eps) {
                result.warnings.push("EPS 会合并透明效果；复杂渐变或透明区域可能按图片保留，建议与 PDF 对照检查。");
                break;
            }
        }
        for (var e=0; e<c.entries.length; e++) {
            var entry = c.entries[e];
            var formats = ["pdf","eps"];
            for (var f=0; f<formats.length; f++) {
                var format = formats[f];
                if (!entry[format]) continue;
                try {
                    var destination = new File(entry[format]);
                    if (destination.fsName === source.fsName || destination.exists) throw new Error("输出文件已存在，已停止以保护原文件。");
                    if (!destination.parent.exists && !destination.parent.create()) throw new Error("无法创建输出文件夹。");
                    doc = app.open(source);
                    prepare(entry);
                    if (format === "pdf") savePDF(destination); else saveEPS(destination);
                    if (!destination.exists || !destination.length) throw new Error("Illustrator 未生成有效的输出文件。");
                    result.outputs.push({index:entry.index,format:format,path:destination.fsName,width_pt:entry.width_pt,height_pt:entry.height_pt});
                } catch (error) {
                    result.errors.push("画板 " + entry.index + " 的 " + format.toUpperCase() + " 导出失败：" + String(error.message || error));
                } finally {
                    if (doc) { try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (ignored) {} doc = null; }
                }
            }
        }
        result.success = result.errors.length === 0 && result.outputs.length > 0;
    } catch (error) {
        result.errors.push(String(error.message || error));
    } finally {
        if (doc) { try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (ignored) {} }
        if (previous) { try { app.activeDocument = previous; } catch (ignored) {} }
        app.userInteractionLevel = interaction;
        app.coordinateSystem = coordinates;
        if (c.result) {
            var resultFile = new File(c.result);
            resultFile.encoding = "UTF-8";
            if (resultFile.open("w")) { resultFile.write(encode(result)); resultFile.close(); }
        }
    }
    return encode(result);
})();
