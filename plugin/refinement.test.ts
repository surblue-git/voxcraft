import { assessRefinementSafety, preserveParagraphBreaks } from "./refinement";

function equal(actual: string, expected: string): void {
    if (actual !== expected) {
        throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    }
}

function safety(
    provisional: string,
    refined: string,
    expected: boolean,
    name: string,
): void {
    const result = assessRefinementSafety(provisional, refined);
    if (result.safe !== expected) {
        throw new Error(`${name}: expected ${expected}, got ${result.safe} (${result.reason ?? "safe"})`);
    }
}

equal(
    preserveParagraphBreaks("新しい第一文。新しい第二文。", "古い第一文。\n\n古い第二文。"),
    "新しい第一文。\n\n新しい第二文。",
);
equal(
    preserveParagraphBreaks("補正後の冒頭です。", "\n\n速報の冒頭です。"),
    "\n\n補正後の冒頭です。",
);
equal(
    preserveParagraphBreaks("第一です。第二です。第三です。", "甲です。\n\n乙です。\n\n丙です。"),
    "第一です。\n\n第二です。\n\n第三です。",
);
equal(
    preserveParagraphBreaks("改行のない補正稿です。", "改行のない速報稿です。"),
    "改行のない補正稿です。",
);
equal(
    preserveParagraphBreaks("サービスを提供します。", "サ\n\nービスを提供します。"),
    "サービスを提供します。",
);
equal(
    preserveParagraphBreaks(
        "魅力的な商品を提供します。続いて新商品を紹介します。",
        "魅力的な商品を\n\n提供します。続いて新商品を紹介します。",
    ),
    "魅力的な商品を提供します。\n\n続いて新商品を紹介します。",
);

safety(
    "皆さんこんにちは。本日はお集まりいただきありがとうございます。アメリカンエクスプレスの須藤です。",
    "皆さん、こんにちは。本日はお集まりいただき、ありがとうございます。アメリカン・エキスプレスの須藤です。",
    true,
    "allows local corrections",
);

safety(
    "皆さんこんにちは本日はご多忙の中お集まりいただきまして誠にありがとうございます。アメリカン・エキスプレスの須藤です。私どもアメリカン・エキスプレスは日々世界最高の顧客体験を提供するというビジョンのもとで決済にとどまらずカード会員の皆様にトラベルそしてダイニングエンターテインメントを通した特別な体験価値を提供することに日々取り組んでおります。",
    "アメリカン・エキスプレスカード会員の皆様にトラベルそしてダイニングエンターテインメントを通した特別な体験価値を提供することに日々取り組んでおります。",
    false,
    "rejects leading content loss",
);

safety(
    "プラチナカードが日本で最初に発行されたのは1993年10月です。当時は世界各地のホテルでの優待やコンシェルジュサービスを提供していました。",
    "プラチナカードが日本で最初に発行されました。",
    false,
    "rejects dramatically shorter refinement",
);

safety(
    "旅行の出発前から旅行先まで、ダイニングやエンターテインメントなど、あらゆる場面で特別な体験価値を提供することを大切にしています。",
    "旅行の出発前から旅行先まで、ダイニングやエンターテインメントなど、あらゆる場面で",
    false,
    "rejects trailing content loss",
);

console.log("refinement tests passed");
