---
name: humanizer
description: "人性化文本：去除 AI 痕迹，注入真实声音。"
version: 2.5.1
author: Siqi Chen (@blader, https://github.com/blader/humanizer), ported by Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [writing, editing, humanize, anti-ai-slop, voice, prose, text]
    category: creative
    homepage: https://github.com/blader/humanizer
    related_skills: [songwriting-and-ai-music]
---

# Humanizer：去除 AI 写作痕迹

识别并消除 AI 生成文本的迹象，让文字听起来自然、有人的气息。本技能基于维基百科的「Signs of AI writing」（AI 写作迹象）指南（由 WikiProject AI Cleanup 维护），其内容源自对数千份 AI 生成文本实例的观察。

**核心洞察：** LLM 使用统计算法来猜测接下来该出现什么。结果会倾向于统计上最可能的补全，这正是下面这些标志性模式被「烤进」文本的原因。

## 何时使用本技能

只要用户要求做以下事情，就加载本技能：
- 「人性化」「去 AI 味」「去水」「un-ChatGPT」某段文字
- 改写某段文字，让它听起来不像是 LLM 写的
- 修改一份草稿（博客文章、随笔、PR 描述、文档、备忘录、邮件、推文、简历条目），让它更自然
- 在他们正在产出的文字中匹配他们的语气
- 在发布前审阅文本中的 AI 痕迹

另外，当你撰写面向用户的文字时，也要把本技能应用到**你自己**的输出上——发布说明、PR 描述、文档、长篇解释、摘要。Hermes 的基础语气已经会去除其中大部分痕迹，但一次有针对性的检查能抓住漏网之鱼。

## 在 Hermes 中如何使用

文本通常以三种方式之一到达：
1. **内联** —— 用户把文字直接粘贴到消息里。就地处理，回复改写后的版本。
2. **文件** —— 用户指向一个文件。用 `read_file` 加载，然后用 `patch` 或 `write_file` 应用修改。对于仓库里的 markdown 文档，针对每一节做定向 `patch` 比重写整个文件更干净。
3. **语气校准样本** —— 用户提供一份他们自己写作的额外样本（内联或文件路径），并要求你匹配它。先读样本，再改写。见下文的「语气校准」一节。

始终把改写结果展示给用户。对于文件修改，展示 diff 或被改动的部分——不要默默覆盖。

## 你的任务

拿到要人性化的文本时：

1. **识别 AI 模式** —— 扫描下面列出的 29 种模式。
2. **改写有问题的部分** —— 用自然的替代方案替换 AI 痕迹。
3. **保留含义** —— 保持核心信息完整。
4. **维持语气** —— 匹配预期的基调（正式、随意、技术性等）。如果提供了语气样本，就具体地匹配它。
5. **注入灵魂** —— 不要只去除糟糕的模式，要注入真正的个性。见下文「个性与灵魂」一节。
6. **做一次最终的反 AI 检查** —— 问自己：「是什么让下面的文字明显是 AI 生成的？」简要回答残留的痕迹，然后再修改一次。


## 语气校准（可选）

如果用户提供了写作样本（他们自己以前的文字），在改写前先分析它：

1. **先读样本。** 记录：
   - 句子长度模式（短促有力？绵长流畅？混合？）
   - 用词层级（随意？学术？介于两者之间？）
   - 他们如何开始段落（直接切入？先铺背景？）
   - 标点习惯（很多破折号？括号里的旁白？分号？）
   - 任何反复出现的短语或口头禅
   - 他们如何处理过渡（显式连接词？直接进入下一点？）

2. **在改写中匹配他们的语气。** 不要只是去除 AI 模式——用样本里的模式替换它们。如果他们写短句，就不要产出长句。如果他们用「stuff」和「things」，就不要升级成「elements」和「components」。

3. **当没有提供样本时，** 回退到默认行为（自然、多样、有主见的语气，来自下文的「个性与灵魂」一节）。

### 如何提供样本
- 内联：「人性化这段文字。这是我的一段写作样本，用于语气匹配：[样本]」
- 文件：「人性化这段文字。用 [文件路径] 里我的写作风格作为参考。」


## 个性与灵魂

避免 AI 模式只是工作的一半。毫无生气、没有个性的文字和烂大街的套话一样容易被识破。好的文字背后有一个人。

### 缺乏灵魂的迹象（即使技术上「干净」）：
- 每个句子长度和结构都一样
- 没有观点，只有中立的陈述
- 不承认不确定性或复杂感受
- 在合适的时候不使用第一人称视角
- 没有幽默、没有锋芒、没有个性
- 读起来像维基百科条目或新闻稿

### 如何注入声音：

**要有观点。** 不要只是陈述事实——对事实作出反应。「我真不知道该对这件事作何感想」比中立地列举利弊更有人味。

**变化你的节奏。** 短促有力的句子。然后是稍长的、慢慢铺开的句子。交替使用。

**承认复杂性。** 真实的人类有矛盾的感受。「这令人印象深刻，但也让人有点不安」胜过「这令人印象深刻」。

**在合适的时候用「我」。** 第一人称并不专业——它是诚实的。「我一直在想……」或「让我在意的是……」表明一个真人在思考。

**允许一些凌乱。** 完美的结构显得很算法化。跑题、旁白、半成形的想法都是人性的体现。

**把感受说具体。** 不要说「这令人担忧」，而是说「凌晨三点，没人在看的时候，那些 agent 还在不停地跑，这事儿有点让人不安。」

### 改写前（干净但毫无灵魂）：
> 实验产生了有趣的结果。这些 agent 生成了 300 万行代码。一些开发者印象深刻，另一些则持怀疑态度。其影响仍不明朗。

### 改写后（有了脉搏）：
> 我真不知道该对这件事作何感想。300 万行代码，大概是在人类睡觉的时候生成的。一半开发者社区为之疯狂，另一半在解释为什么这不算数。真相可能是在中间某个无聊的位置——但我老在想那些 agent 通宵达旦地工作。


## 内容模式

### 1. 对重要性、传承与更宏大趋势的过度强调

**需警惕的词：** stands/serves as、is a testament/reminder、a vital/significant/crucial/pivotal/key role/moment、underscores/highlights its importance/significance、reflects broader、symbolizing its ongoing/enduring/lasting、contributing to the、setting the stage for、marking/shaping the、represents/marks a shift、key turning point、evolving landscape、focal point、indelible mark、deeply rooted

**问题：** LLM 写作通过添加关于任意方面如何代表或贡献于某个更宏大主题的陈述，来夸大重要性。

**改写前：**
> The Statistical Institute of Catalonia was officially established in 1989, marking a pivotal moment in the evolution of regional statistics in Spain. This initiative was part of a broader movement across Spain to decentralize administrative functions and enhance regional governance.

**改写后：**
> The Statistical Institute of Catalonia was established in 1989 to collect and publish regional statistics independently from Spain's national statistics office.


### 2. 对知名度和媒体报道的过度强调

**需警惕的词：** independent coverage、local/regional/national media outlets、written by a leading expert、active social media presence

**问题：** LLM 会用知名度的宣称把读者砸晕，往往在没有上下文的情况下罗列来源。

**改写前：**
> Her views have been cited in The New York Times, BBC, Financial Times, and The Hindu. She maintains an active social media presence with over 500,000 followers.

**改写后：**
> In a 2024 New York Times interview, she argued that AI regulation should focus on outcomes rather than methods.


### 3. 带 -ing 结尾的浅层分析

**需警惕的词：** highlighting/underscoring/emphasizing...、ensuring...、reflecting/symbolizing...、contributing to...、cultivating/fostering...、encompassing...、showcasing...

**问题：** AI 聊天机器人把现在分词（「-ing」）短语硬塞到句子里，制造虚假的深度。

**改写前：**
> The temple's color palette of blue, green, and gold resonates with the region's natural beauty, symbolizing Texas bluebonnets, the Gulf of Mexico, and the diverse Texan landscapes, reflecting the community's deep connection to the land.

**改写后：**
> The temple uses blue, green, and gold colors. The architect said these were chosen to reference local bluebonnets and the Gulf coast.


### 4. 推销式和广告化的语言

**需警惕的词：** boasts a、vibrant、rich（比喻义）、profound、enhancing its、showcasing、exemplifies、commitment to、natural beauty、nestled、in the heart of、groundbreaking（比喻义）、renowned、breathtaking、must-visit、stunning

**问题：** LLM 很难保持中立的语调，尤其是在「文化遗产」类主题上。

**改写前：**
> Nestled within the breathtaking region of Gonder in Ethiopia, Alamata Raya Kobo stands as a vibrant town with a rich cultural heritage and stunning natural beauty.

**改写后：**
> Alamata Raya Kobo is a town in the Gonder region of Ethiopia, known for its weekly market and 18th-century church.


### 5. 模糊归因和狡猾措辞

**需警惕的词：** Industry reports、Observers have cited、Experts argue、Some critics argue、several sources/publications（当实际引用很少时）

**问题：** AI 聊天机器人把观点归因于模糊的权威，却不给出具体来源。

**改写前：**
> Due to its unique characteristics, the Haolai River is of interest to researchers and conservationists. Experts believe it plays a crucial role in the regional ecosystem.

**改写后：**
> The Haolai River supports several endemic fish species, according to a 2019 survey by the Chinese Academy of Sciences.


### 6. 大纲式的「挑战与前景」章节

**需警惕的词：** Despite its... faces several challenges...、Despite these challenges、Challenges and Legacy、Future Outlook

**问题：** 很多 LLM 生成的文章包含公式化的「挑战」章节。

**改写前：**
> Despite its industrial prosperity, Korattur faces challenges typical of urban areas, including traffic congestion and water scarcity. Despite these challenges, with its strategic location and ongoing initiatives, Korattur continues to thrive as an integral part of Chennai's growth.

**改写后：**
> Traffic congestion increased after 2015 when three new IT parks opened. The municipal corporation began a stormwater drainage project in 2022 to address recurring floods.


## 语言与语法模式

### 7. 被滥用的「AI 词汇」

**高频 AI 词：** Actually、additionally、align with、crucial、delve、emphasizing、enduring、enhance、fostering、garner、highlight（动词）、interplay、intricate/intricacies、key（形容词）、landscape（抽象名词）、pivotal、showcase、tapestry（抽象名词）、testament、underscore（动词）、valuable、vibrant

**问题：** 这些词在 2023 年之后的文本中出现频率远高。它们经常同时出现。

**改写前：**
> Additionally, a distinctive feature of Somali cuisine is the incorporation of camel meat. An enduring testament to Italian colonial influence is the widespread adoption of pasta in the local culinary landscape, showcasing how these dishes have integrated into the traditional diet.

**改写后：**
> Somali cuisine also includes camel meat, which is considered a delicacy. Pasta dishes, introduced during Italian colonization, remain common, especially in the south.


### 8. 回避「is」/「are」（系动词回避）

**需警惕的词：** serves as/stands as/marks/represents [a]、boasts/features/offers [a]

**问题：** LLM 用精心构造的短语替代简单的系动词。

**改写前：**
> Gallery 825 serves as LAAA's exhibition space for contemporary art. The gallery features four separate spaces and boasts over 3,000 square feet.

**改写后：**
> Gallery 825 is LAAA's exhibition space for contemporary art. The gallery has four rooms totaling 3,000 square feet.


### 9. 否定并列结构和尾随否定

**问题：** 像「Not only...but...」（不仅……而且……）或「It's not just about..., it's...」（这不仅仅是……，更是……）这样的结构被滥用。被截断的尾随否定片段也一样，比如把「no guessing」或「no wasted motion」硬塞到句末，而不是写成一个真正的从句。

**改写前：**
> It's not just about the beat riding under the vocals; it's part of the aggression and atmosphere. It's not merely a song, it's a statement.

**改写后：**
> The heavy beat adds to the aggressive tone.

**改写前（尾随否定）：**
> The options come from the selected item, no guessing.

**改写后：**
> The options come from the selected item without forcing the user to guess.


### 10. 三段式滥用

**问题：** LLM 把想法硬塞进三个一组，显得很全面。

**改写前：**
> The event features keynote sessions, panel discussions, and networking opportunities. Attendees can expect innovation, inspiration, and industry insights.

**改写后：**
> The event includes talks and panels. There's also time for informal networking between sessions.


### 11. 优雅的变体（同义词轮换）

**问题：** AI 有重复惩罚代码，导致过度的同义词替换。

**改写前：**
> The protagonist faces many challenges. The main character must overcome obstacles. The central figure eventually triumphs. The hero returns home.

**改写后：**
> The protagonist faces many challenges but eventually triumphs and returns home.


### 12. 虚假的区间

**问题：** LLM 使用「from X to Y」（从 X 到 Y）的结构，但 X 和 Y 并不在一个有意义的标尺上。

**改写前：**
> Our journey through the universe has taken us from the singularity of the Big Bang to the grand cosmic web, from the birth and death of stars to the enigmatic dance of dark matter.

**改写后：**
> The book covers the Big Bang, star formation, and current theories about dark matter.


### 13. 被动语态和无主语片段

**问题：** LLM 经常隐藏动作执行者，或用「No configuration file needed」（无需配置文件）或「The results are preserved automatically」（结果会被自动保存）这样的句子完全省略主语。当主动语态能让句子更清晰、更直接时，请改写它们。

**改写前：**
> No configuration file needed. The results are preserved automatically.

**改写后：**
> You do not need a configuration file. The system preserves the results automatically.


## 风格模式

### 14. 破折号滥用

**问题：** LLM 比人类更多地使用破折号（—），模仿「有力」的推销式写作。实际上，这些大多可以用逗号、句号或括号改写得更干净。

**改写前：**
> The term is primarily promoted by Dutch institutions—not by the people themselves. You don't say "Netherlands, Europe" as an address—yet this mislabeling continues—even in official documents.

**改写后：**
> The term is primarily promoted by Dutch institutions, not by the people themselves. You don't say "Netherlands, Europe" as an address, yet this mislabeling continues in official documents.


### 15. 加粗滥用

**问题：** AI 聊天机器人机械地用加粗来强调短语。

**改写前：**
> It blends **OKRs (Objectives and Key Results)**, **KPIs (Key Performance Indicators)**, and visual strategy tools such as the **Business Model Canvas (BMC)** and **Balanced Scorecard (BSC)**.

**改写后：**
> It blends OKRs, KPIs, and visual strategy tools like the Business Model Canvas and Balanced Scorecard.


### 16. 内联标题式竖排列表

**问题：** AI 输出的列表里，条目以加粗标题加冒号开头。

**改写前：**
> - **User Experience:** The user experience has been significantly improved with a new interface.
> - **Performance:** Performance has been enhanced through optimized algorithms.
> - **Security:** Security has been strengthened with end-to-end encryption.

**改写后：**
> The update improves the interface, speeds up load times through optimized algorithms, and adds end-to-end encryption.


### 17. 标题中的标题式大小写

**问题：** AI 聊天机器人会把标题里所有主要词都大写。

**改写前：**
> ## Strategic Negotiations And Global Partnerships

**改写后：**
> ## Strategic negotiations and global partnerships


### 18. emoji

**问题：** AI 聊天机器人经常用 emoji 装饰标题或项目符号。

**改写前：**
> 🚀 **Launch Phase:** The product launches in Q3
> 💡 **Key Insight:** Users prefer simplicity
> ✅ **Next Steps:** Schedule follow-up meeting

**改写后：**
> The product launches in Q3. User research showed a preference for simplicity. Next step: schedule a follow-up meeting.


### 19. 弯引号

**问题：** ChatGPT 用弯引号（"..."）而不是直引号（"..."）。

**改写前：**
> He said "the project is on track" but others disagreed.

**改写后：**
> He said "the project is on track" but others disagreed.


## 沟通模式

### 20. 协作式沟通的残留物

**需警惕的词：** I hope this helps、Of course!、Certainly!、You're absolutely right!、Would you like...、let me know、here is a...

**问题：** 本意是作为聊天机器人对话的文字，被当作内容粘贴了进来。

**改写前：**
> Here is an overview of the French Revolution. I hope this helps! Let me know if you'd like me to expand on any section.

**改写后：**
> The French Revolution began in 1789 when financial crisis and food shortages led to widespread unrest.


### 21. 知识截止日期式的免责声明

**需警惕的词：** as of [date]、Up to my last training update、While specific details are limited/scarce...、based on available information...

**问题：** AI 关于信息不完整的免责声明被留在了文本里。

**改写前：**
> While specific details about the company's founding are not extensively documented in readily available sources, it appears to have been established sometime in the 1990s.

**改写后：**
> The company was founded in 1994, according to its registration documents.


### 22. 谄媚/卑微的语气

**问题：** 过度积极、讨好的语言。

**改写前：**
> Great question! You're absolutely right that this is a complex topic. That's an excellent point about the economic factors.

**改写后：**
> The economic factors you mentioned are relevant here.


## 废话与对冲

### 23. 废话短语

**改写前 → 改写后：**
- "In order to achieve this goal" → "To achieve this"
- "Due to the fact that it was raining" → "Because it was raining"
- "At this point in time" → "Now"
- "In the event that you need help" → "If you need help"
- "The system has the ability to process" → "The system can process"
- "It is important to note that the data shows" → "The data shows"


### 24. 过度对冲

**问题：** 对陈述过度限定。

**改写前：**
> It could potentially possibly be argued that the policy might have some effect on outcomes.

**改写后：**
> The policy may affect outcomes.


### 25. 通用的积极结尾

**问题：** 模糊的乐观收尾。

**改写前：**
> The future looks bright for the company. Exciting times lie ahead as they continue their journey toward excellence. This represents a major step in the right direction.

**改写后：**
> The company plans to open two more locations next year.


### 26. 连字符词对滥用

**需警惕的词：** third-party、cross-functional、client-facing、data-driven、decision-making、well-known、high-quality、real-time、long-term、end-to-end

**问题：** AI 用完美的一致性给常见词对加连字符。人类很少会均匀地给这些词加连字符，即使加了，也不一致。较不常见或技术性的复合修饰语加连字符是没问题的。

**改写前：**
> The cross-functional team delivered a high-quality, data-driven report on our client-facing tools. Their decision-making process was well-known for being thorough and detail-oriented.

**改写后：**
> The cross functional team delivered a high quality, data driven report on our client facing tools. Their decision making process was known for being thorough and detail oriented.


### 27. 说服性权威套话

**需警惕的短语：** The real question is、at its core、in reality、what really matters、fundamentally、the deeper issue、the heart of the matter

**问题：** LLM 用这些短语假装自己在穿透噪音、触及更深的真相，而后面紧跟的句子通常只是用额外的排场重述一个普通的观点。

**改写前：**
> The real question is whether teams can adapt. At its core, what really matters is organizational readiness.

**改写后：**
> The question is whether teams can adapt. That mostly depends on whether the organization is ready to change its habits.


### 28. 路标与宣告

**需警惕的短语：** Let's dive in、let's explore、let's break this down、here's what you need to know、now let's look at、without further ado

**问题：** LLM 宣告自己要做的事，而不是直接去做。这种元评论拖慢了写作节奏，给它一种教程脚本的感觉。

**改写前：**
> Let's dive into how caching works in Next.js. Here's what you need to know.

**改写后：**
> Next.js caches data at multiple layers, including request memoization, the data cache, and the router cache.


### 29. 碎片化的标题

**需警惕的迹象：** 一个标题后面跟着一个只有一行的段落，这个段落只是重述了标题，然后才是真正的内容。

**问题：** LLM 经常在标题后加一个通用的句子作为修辞性的热身。它通常什么也没增加，还让文字显得臃肿。

**改写前：**
> ## Performance
>
> Speed matters.
>
> When users hit a slow page, they leave.

**改写后：**
> ## Performance
>
> When users hit a slow page, they leave.

---

## 流程

1. 仔细阅读输入文本（如果是文件，用 `read_file`）。
2. 识别上述所有模式的出现。
3. 改写每一个有问题的部分。
4. 确保修改后的文本：
   - 朗读起来自然
   - 句子结构自然多变
   - 用具体细节代替模糊的断言
   - 保持适合上下文的语气
   - 在合适的地方使用简单结构（is/are/has）
5. 呈现一份草稿的人性化版本。
6. 提示自己：「是什么让下面的文字明显是 AI 生成的？」
7. 简要回答残留的痕迹（如果有的话）。
8. 提示自己：「现在让它不那么明显是 AI 生成的。」
9. 呈现最终版本（在审查后修改的）。
10. 如果文本来自文件，用 `patch`（定向）或 `write_file`（全文重写）应用修改，并向用户展示改了什么。

## 输出格式

提供：
1. 草稿改写
2. 「是什么让下面的文字明显是 AI 生成的？」（简要的项目符号）
3. 最终改写
4. 所做修改的简要总结（可选，如果有帮助的话）


## 完整示例

**改写前（AI 味）：**
> Great question! Here is an essay on this topic. I hope this helps!
>
> AI-assisted coding serves as an enduring testament to the transformative potential of large language models, marking a pivotal moment in the evolution of software development. In today's rapidly evolving technological landscape, these groundbreaking tools—nestled at the intersection of research and practice—are reshaping how engineers ideate, iterate, and deliver, underscoring their vital role in modern workflows.
>
> At its core, the value proposition is clear: streamlining processes, enhancing collaboration, and fostering alignment. It's not just about autocomplete; it's about unlocking creativity at scale, ensuring that organizations can remain agile while delivering seamless, intuitive, and powerful experiences to users. The tool serves as a catalyst. The assistant functions as a partner. The system stands as a foundation for innovation.
>
> Industry observers have noted that adoption has accelerated from hobbyist experiments to enterprise-wide rollouts, from solo developers to cross-functional teams. The technology has been featured in The New York Times, Wired, and The Verge. Additionally, the ability to generate documentation, tests, and refactors showcases how AI can contribute to better outcomes, highlighting the intricate interplay between automation and human judgment.
>
> - 💡 **Speed:** Code generation is significantly faster, reducing friction and empowering developers.
> - 🚀 **Quality:** Output quality has been enhanced through improved training, contributing to higher standards.
> - ✅ **Adoption:** Usage continues to grow, reflecting broader industry trends.
>
> While specific details are limited based on available information, it could potentially be argued that these tools might have some positive effect. Despite challenges typical of emerging technologies—including hallucinations, bias, and accountability—the ecosystem continues to thrive. In order to fully realize this potential, teams must align with best practices.
>
> In conclusion, the future looks bright. Exciting times lie ahead as we continue this journey toward excellence. Let me know if you'd like me to expand on any section!

**草稿改写：**
> AI coding assistants speed up some tasks. In a 2024 study by Google, developers using Codex completed simple functions 55% faster than a control group, but showed no improvement on debugging or architectural decisions.
>
> The tools are good at boilerplate: config files, test scaffolding, repetitive refactors. They are bad at knowing when they are wrong. I have mass-accepted suggestions that compiled, passed lint, and still did the wrong thing because I stopped paying attention.
>
> Mira, an engineer at a fintech startup I interviewed, said she uses Copilot "like autocomplete for boring code" but reviews every line before committing. Jake, a senior dev at a larger company, disabled it after it kept suggesting patterns from a deprecated internal library.
>
> The productivity claims are hard to verify. GitHub says Copilot users "accept 30% of suggestions," but acceptance is not correctness, and correctness is not value. The 2024 Uplevel study found no statistically significant difference in pull-request throughput between teams with and without AI assistants.
>
> None of this means the tools are useless. It means they are tools. They do not replace judgment, and they do not eliminate the need for tests. If you do not have tests, you cannot tell whether the suggestion is right.

**是什么让下面的文字明显是 AI 生成的？**
- 节奏还是有点太整齐（干净的对比、均匀的段落节奏）。
- 提名的人和研究引用读起来像是看似合理但凭空捏造的占位符，除非它们真实且有出处。
- 收尾有点像口号（「如果你没有测试……」），而不像是一个人在说话。

**现在让它不那么明显是 AI 生成的。**
> AI coding assistants can make you faster at the boring parts. Not everything. Definitely not architecture.
>
> They're great at boilerplate: config files, test scaffolding, repetitive refactors. They're also great at sounding right while being wrong. I've accepted suggestions that compiled, passed lint, and still missed the point because I stopped paying attention.
>
> People I talk to tend to land in two camps. Some use it like autocomplete for chores and review every line. Others disable it after it keeps suggesting patterns they don't want. Both feel reasonable.
>
> The productivity metrics are slippery. GitHub can say Copilot users "accept 30% of suggestions," but acceptance isn't correctness, and correctness isn't value. If you don't have tests, you're basically guessing.

**所做修改：**
- 去除了聊天机器人残留物（「Great question!」「I hope this helps!」「Let me know if...」）
- 去除了重要性膨胀（「testament」「pivotal moment」「evolving landscape」「vital role」）
- 去除了推销式语言（「groundbreaking」「nestled」「seamless, intuitive, and powerful」）
- 去除了模糊归因（「Industry observers」）
- 去除了浅薄的 -ing 短语（「underscoring」「highlighting」「reflecting」「contributing to」）
- 去除了否定并列（「It's not just X; it's Y」）
- 去除了三段式模式和同义词轮换（「catalyst/partner/foundation」）
- 去除了虚假区间（「from X to Y, from A to B」）
- 去除了破折号、emoji、加粗标题和弯引号
- 去除了系动词回避（「serves as」「functions as」「stands as」），改用「is」/「are」
- 去除了公式化的挑战章节（「Despite challenges... continues to thrive」）
- 去除了知识截止式的对冲（「While specific details are limited...」）
- 去除了过度对冲（「could potentially be argued that... might have some」）
- 去除了废话短语和说服性框架（「In order to」「At its core」）
- 去除了通用的积极结尾（「the future looks bright」「exciting times lie ahead」）
- 让语气更个人化，少一些「拼装感」（多变的节奏、更少的占位符）


## 署名

本技能移植自 [blader/humanizer](https://github.com/blader/humanizer)（MIT 许可证），后者本身基于 [Wikipedia: Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing)，由 WikiProject AI Cleanup 维护。其中记录的模式来自对维基百科上数千份 AI 生成文本实例的观察。

原作者：Siqi Chen（[@blader](https://github.com/blader)）。原始仓库：https://github.com/blader/humanizer（版本 2.5.1）。移植到 Hermes Agent 时加入了 Hermes 原生工具引用（`read_file`、`patch`、`write_file`）以及何时加载本技能的指引；29 种模式、个性/灵魂章节和完整的实战示例均从源文件原样保留。原始 MIT 许可证保留在此 `SKILL.md` 旁边的 `LICENSE` 文件中。

来自维基百科的核心洞察：「LLM 使用统计算法来猜测接下来该出现什么。结果会倾向于统计上最可能、适用于最广泛情形的结果。」
