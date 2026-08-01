interface ParagraphMarker {
    offset: number;
    separator: string;
}

function withoutLineBreaks(text: string): string {
    return text.replace(/[\r\n]+/g, "");
}

function paragraphMarkers(text: string): ParagraphMarker[] {
    const markers: ParagraphMarker[] = [];
    const pattern = /(?:\r?\n){2,}/g;
    let match: RegExpExecArray | null;
    while ((match = pattern.exec(text)) !== null) {
        markers.push({
            offset: withoutLineBreaks(text.slice(0, match.index)).length,
            // ParagraphBreaker の出力と同じく、段落間は常に空行1つへそろえる。
            separator: "\n\n",
        });
    }
    return markers;
}

function closestBoundary(
    text: string,
    target: number,
    minimum: number,
): number {
    const clamped = Math.max(minimum, Math.min(text.length, target));
    const radius = Math.max(12, Math.round(text.length * 0.08));

    // まず文末を優先する。見つからない場合だけ読点・空白へ落とし、
    // それも遠ければ元の段落比率をそのまま使う。
    for (const pattern of [/[。！？!?」』）]/g, /[、，,：:；;\s]/g]) {
        let best = -1;
        let distance = Number.POSITIVE_INFINITY;
        let match: RegExpExecArray | null;
        while ((match = pattern.exec(text)) !== null) {
            const position = match.index + match[0].length;
            if (position < minimum) continue;
            const candidateDistance = Math.abs(position - clamped);
            if (candidateDistance < distance) {
                best = position;
                distance = candidateDistance;
            }
        }
        if (best >= 0 && distance <= radius) return best;
    }
    return clamped;
}

/**
 * 速報稿にあった段落区切りを、内容が改善された補正稿へ移植する。
 *
 * 補正認識は30秒の音声を一度に文字列化するため、速報チャンクの前に付けた
 * 空行を持たない。旧稿での相対位置を目安に、補正稿の最寄りの文末へ空行を戻す。
 */
export function preserveParagraphBreaks(refined: string, provisional: string): string {
    const markers = paragraphMarkers(provisional);
    if (markers.length === 0) return refined;

    const flatRefined = withoutLineBreaks(refined);
    const provisionalLength = withoutLineBreaks(provisional).length;
    if (!flatRefined || provisionalLength === 0) return refined;

    const placements: ParagraphMarker[] = [];
    let previous = -1;
    for (const marker of markers) {
        const target = Math.round(
            (marker.offset / provisionalLength) * flatRefined.length,
        );
        const minimum = marker.offset === 0 ? 0 : Math.min(flatRefined.length, previous + 1);
        const position = marker.offset === 0
            ? 0
            : marker.offset >= provisionalLength
                ? flatRefined.length
                : closestBoundary(flatRefined, target, minimum);
        placements.push({ offset: position, separator: marker.separator });
        previous = position;
    }

    let result = flatRefined;
    for (let i = placements.length - 1; i >= 0; i -= 1) {
        const marker = placements[i];
        result = result.slice(0, marker.offset) + marker.separator + result.slice(marker.offset);
    }
    return result;
}
