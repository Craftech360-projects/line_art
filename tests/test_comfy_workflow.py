from app.comfy_workflow import build_flux_workflow


def test_workflow_embeds_prompt_and_dims():
    g = build_flux_workflow("simple line art of a cat", width=768, height=512, steps=4, seed=7)
    # Prompt appears in some CLIPTextEncode node.
    texts = [n["inputs"].get("text") for n in g.values() if n["class_type"] == "CLIPTextEncode"]
    assert "simple line art of a cat" in texts
    # Latent dims set.
    latents = [n for n in g.values() if n["class_type"] == "EmptyLatentImage"]
    assert latents and latents[0]["inputs"]["width"] == 768
    assert latents[0]["inputs"]["height"] == 512
    # Steps + seed on the sampler.
    samplers = [n for n in g.values() if n["class_type"] == "KSampler"]
    assert samplers and samplers[0]["inputs"]["steps"] == 4
    assert samplers[0]["inputs"]["seed"] == 7


def test_workflow_has_save_node_and_checkpoint():
    g = build_flux_workflow("x")
    assert g["save"]["class_type"] == "SaveImage"
    ckpts = [n for n in g.values() if n["class_type"] == "CheckpointLoaderSimple"]
    assert ckpts and "flux1-schnell-fp8" in ckpts[0]["inputs"]["ckpt_name"]
