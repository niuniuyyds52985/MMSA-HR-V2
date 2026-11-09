"""Behaviour checks for AU + SER state inference, separate from score arithmetic."""
import copy
import unittest

from src.mmsa_v2.affect_fusion import estimate, face_band, speech_band, fuse_bands


def au_row(kind="neutral", start=0, end=5000):
    intensities = {"AU06":0.0,"AU12":0.0,"AU15":0.0,"AU01":0.0,"AU04":0.0}
    values = {"mean_intensity":intensities,"presence_fraction":{},"patterns":{},
              "low_activation_aus":list(intensities),"low_activation_fraction":1.0}
    if kind != "neutral":
        values["low_activation_fraction"] = 0.0
    if kind in ("happy", "strong_happy", "conflict"):
        intensities.update(AU06=1.0, AU12=2.5 if kind=="strong_happy" else 1.2)
        values["patterns"]["happy"]={"matched_fraction":1.0}
    if kind in ("sad", "strong_sad", "conflict"):
        intensities.update(AU01=1.0, AU04=1.0, AU15=2.6 if kind=="strong_sad" else 1.2)
        values["patterns"]["sad"]={"matched_fraction":1.0}
    if kind == "surprise":
        values["patterns"]["surprise"]={"matched_fraction":1.0}
    return {"start_ms":start,"end_ms":end,"quality":"valid","coverage":1.0,"values":values}


def voice_row(label="neutral", start=0, end=5000, confidence=.8):
    other = "neutral" if label != "neutral" else "happy"
    return {"start_ms":start,"end_ms":end,"quality":"valid",
            "values":{"label":label,"scores":{label:confidence,other:1-confidence}}}


def acoustic_row(start=0, end=5000):
    return {"start_ms":start,"end_ms":end,"quality":"valid","quality_flags":[],
            "values":{"rms_dbfs":-25,"speech_fraction":1.0}}


def run(au=None, voice=None, acoustic=None, vad=None, duration=5000):
    return estimate({"model_id":"AU-fixture","observations":[au if au is not None else au_row()]},
                    {"model_id":"SER-fixture","observations":[voice if voice is not None else voice_row()]},
                    {"observations":[acoustic if acoustic is not None else acoustic_row()],
                     "summary":{"speech_intervals":vad if vad is not None else [{"start_ms":0,"end_ms":duration}]}},
                    duration,"fixture-media-sha")


class AffectFusionTests(unittest.TestCase):
    def test_au_happy_is_not_vetoed_by_legacy_fer_neutral_or_disgust(self):
        for legacy_fer in ("neutral","disgust","sad",None):
            with self.subTest(legacy_fer=legacy_fer):
                face=au_row("happy")
                face["values"]["legacy_fer_label"]=legacy_fer
                self.assertEqual(face_band(face)[0],1)
                result=run(au=face)
                self.assertEqual(result["segments"],[{"start":0.0,"end":5.0,"state":"饱满"}])

    def test_voice_happy_changes_neutral_face_to_positive(self):
        result=run(voice=voice_row("happy"))
        self.assertEqual(result["segments"][0]["state"],"饱满")
        evidence=result["evidence"][0]["values"]
        self.assertEqual(evidence["face_state"],"平和")
        self.assertEqual(evidence["speech_state"],"饱满")
        self.assertEqual((evidence["speech_start"],evidence["speech_end"]),(0,5))

    def test_opposite_directions_remain_unclassified_not_averaged_neutral(self):
        for face, voice in (("happy","sad"),("strong_happy","angry"),("sad","happy")):
            with self.subTest(face=face,voice=voice):
                result=run(au=au_row(face),voice=voice_row(voice))
                self.assertEqual(result["segments"],[])
                self.assertIsNone(result["evidence"][0]["state"])
                self.assertIn("冲突",result["evidence"][0]["reason"])
                self.assertEqual(result["quality"]["valid_seconds"],0)

    def test_unknown_direction_does_not_default_to_neutral(self):
        result=run(au=au_row("surprise"),voice=voice_row("surprised"))
        self.assertEqual(result["segments"],[])
        self.assertIsNone(face_band(au_row("unmatched"))[0])
        self.assertIsNone(speech_band(voice_row("unknown"))[0])
        self.assertIsNone(fuse_bands(None,None)[0])
        # Neutral is a real, explicit observation, not a fallback for missing models.
        self.assertEqual(run()["segments"][0]["state"],"平和")

    def test_surprise_pattern_is_not_overridden_by_unrelated_low_activation_aus(self):
        face=au_row("surprise")
        face["values"]["low_activation_fraction"]=1.0
        # The low-activation AU list can exclude the surprise AUs; a matched
        # undirected pattern must still take precedence over this neutral cue.
        self.assertIsNone(face_band(face)[0])

    def test_quality_gates_preserve_missing_time(self):
        scenarios=[]
        face=au_row("happy");face["quality"]="missing"
        scenarios.append({"au":face})
        face=au_row("happy");face["coverage"]=.99
        scenarios.append({"au":face})
        voice=voice_row("happy");voice["quality"]="missing"
        scenarios.append({"voice":voice})
        quiet=acoustic_row();quiet["values"]["rms_dbfs"]=-66
        scenarios.append({"acoustic":quiet})
        for flag in ("audio_clipping","near_silence"):
            audio=acoustic_row();audio["quality_flags"]=[flag]
            scenarios.append({"acoustic":audio})
        invalid=acoustic_row();invalid["values"]["rms_dbfs"]=float("nan")
        scenarios.append({"acoustic":invalid})
        scenarios.append({"vad":[]})
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                result=run(**scenario)
                self.assertEqual(result["segments"],[])
                self.assertEqual(result["quality"]["valid_seconds"],0)
                self.assertEqual(result["quality"]["unclassified_seconds"],5)

    def test_exact_vad_intersections_do_not_expand_to_full_audio_seconds(self):
        aus=[au_row("happy",0,1000),au_row("happy",1000,2500),au_row("happy",2500,5000)]
        voices=[voice_row("happy",0,2300),voice_row("happy",2300,5000)]
        audio=[acoustic_row(t,t+1000) for t in range(0,5000,1000)]
        vad=[{"start_ms":a,"end_ms":b} for a,b in ((105,255),(1100,1350),(1995,2417),(3500,3880))]
        result=estimate({"observations":aus},{"observations":voices},
                        {"observations":audio,"summary":{"speech_intervals":vad}},5000,"sha")
        self.assertEqual(result["quality"]["valid_seconds"],1.202)
        self.assertAlmostEqual(result["quality"]["coverage"],1.202/5)
        self.assertEqual([(r["start"],r["end"]) for r in result["segments"]],
                         [(.105,.255),(1.1,1.35),(1.995,2.417),(3.5,3.88)])
        rows=result["evidence"]
        self.assertTrue(all(a["end"] <= b["start"] for a,b in zip(rows,rows[1:])))
        self.assertTrue(all(any(v["start_ms"]<=r["start"]*1000 and r["end"]*1000<=v["end_ms"]
                                for v in vad) for r in rows if r["state"] is not None))
        # The SER time window stays distinct from each smaller aligned evidence row.
        voiced=next(r for r in rows if r["state"] is not None)
        self.assertEqual(voiced["values"]["speech_end"],2.3)

    def test_evidence_accounts_for_every_second_including_silence_and_model_gaps(self):
        result=estimate({"observations":[au_row("happy",500,4500)]},
                        {"observations":[voice_row("happy",0,4000)]},
                        {"observations":[acoustic_row()],"summary":{"speech_intervals":[{"start_ms":1000,"end_ms":3000}]}},
                        5000,"sha")
        rows=result["evidence"]
        self.assertEqual((rows[0]["start"],rows[-1]["end"]),(0,5))
        self.assertTrue(all(a["end"]==b["start"] for a,b in zip(rows,rows[1:])))
        self.assertAlmostEqual(sum(r["end"]-r["start"] for r in rows),5)
        self.assertEqual(result["quality"]["valid_seconds"],2)
        self.assertTrue(all(r["reason"] for r in rows if r["state"] is None))

    def test_ser_vad_and_original_audio_vad_are_both_required_when_available(self):
        voice=voice_row("happy")
        voice["speech_intervals"]=[{"start_ms":1200,"end_ms":1500}]
        result=run(voice=voice,vad=[{"start_ms":1000,"end_ms":2000}])
        self.assertEqual(result["quality"]["valid_seconds"],.3)
        self.assertEqual(result["segments"],[{"start":1.2,"end":1.5,"state":"饱满"}])
        voice["speech_intervals"]=[]
        self.assertEqual(run(voice=voice)["quality"]["valid_seconds"],0)
        del voice["speech_intervals"]
        self.assertEqual(run(voice=voice)["quality"]["valid_seconds"],5)

    def test_overlapping_source_windows_are_rejected(self):
        good_au={"observations":[au_row()]}
        good_voice={"observations":[voice_row()]}
        good_audio={"observations":[acoustic_row()],"summary":{"speech_intervals":[{"start_ms":0,"end_ms":5000}]}}
        for target in ("au","voice","audio","vad"):
            au,voice,audio=copy.deepcopy((good_au,good_voice,good_audio))
            rows=[{"start_ms":0,"end_ms":3000},{"start_ms":2500,"end_ms":5000}]
            if target=="au":au["observations"]=rows
            if target=="voice":voice["observations"]=rows
            if target=="audio":audio["observations"]=rows
            if target=="vad":audio["summary"]["speech_intervals"]=rows
            with self.subTest(target=target),self.assertRaises(ValueError):
                estimate(au,voice,audio,5000,"sha")

    def test_classification_score_does_not_become_emotion_intensity(self):
        for confidence in (.51,.8,.9999):
            with self.subTest(confidence=confidence):
                self.assertEqual(run(voice=voice_row("happy",confidence=confidence))["segments"][0]["state"],"饱满")
                self.assertEqual(run(voice=voice_row("sad",confidence=confidence))["segments"][0]["state"],"略微 down")
                self.assertEqual(run(au=au_row("strong_happy"),voice=voice_row("happy",confidence=confidence))["segments"][0]["state"],"洋溢")

    def test_internal_au_conflict_is_not_erased_by_voice_fallback(self):
        for label in ("happy","neutral","sad"):
            with self.subTest(label=label):
                result=run(au=au_row("conflict"),voice=voice_row(label))
                self.assertEqual(result["segments"],[])
                self.assertIn("冲突",result["evidence"][0]["reason"])

    def test_duration_must_be_positive_finite(self):
        for duration in (0,-1,float("nan"),float("inf"),True):
            with self.subTest(duration=duration),self.assertRaises(ValueError):
                estimate({}, {}, {}, duration, "sha")


if __name__=="__main__":
    unittest.main()
